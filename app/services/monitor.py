"""直播监控调度器 - 定时检测房间状态并自动录制"""
import asyncio
import os
import json
import time
import logging
from datetime import timedelta
from typing import Optional
import httpx
from sqlalchemy import select, update, delete
from app.database import async_session
from app.models import Room, Recording, SystemLog
from app.config import settings
from app.utils import utcnow
from app.services import archive
from app.services.recorder import recorder
from app.services.file_manager import file_manager
from app.services.platform import RoomInfo
from app.services.platform_manager import platform_manager

logger = logging.getLogger(__name__)


class LiveMonitor:
    """直播监控调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._room_states: dict[int, dict] = {}
        # 串行化所有重连/换地址操作，避免刷新循环与断流重连同时对同一房间重启 ffmpeg
        self._reconnect_lock = asyncio.Lock()
        # P0-7: 每房间一把锁，串行化 start/reconnect/finalize。
        # 原实现场次状态"读 DB 标志→决策→动作"全程无锁，_finalize_session 可被
        # 监控循环 / check_room_now / 手动停止 / _handle_finished_recordings 四路并发进入：
        # 两路都 select 同一 recording 行、都对同一 final_path 跑 ffmpeg concat(-y)、
        # 都在成功后删源 part → 输出被交错写坏、part 在读取中被删除。
        self._room_locks: dict[int, asyncio.Lock] = {}
        # 上次清理旧系统日志的时间戳（P1-4，按天节流）
        self._last_log_cleanup: float = 0.0
        # 当日跨场次合并串行化（ffmpeg concat 可能持续数十秒到分钟级）
        self._merge_lock = asyncio.Lock()
        # 后台任务强引用：事件循环只持弱引用，不保存会被垃圾回收中断
        self._bg_tasks: set = set()
        # 磁盘水位告警节流（避免每轮监控刷屏）
        self._last_disk_warn: float = 0.0
        # P0-8: 连续探测失败计数。适配器把"网络失败/风控/空数据"与"真下播"都压成
        # is_live=False，单次抖动就会被误判为下播、把在录场次切碎。这里要求连续
        # N 次探测失败（或确认下播）才允许 finalize。
        self._probe_fail_counts: dict[int, int] = {}
        # 断流重连尝试次数（P1-12：避免磁盘满/流地址永久失效时每轮重启 ffmpeg）
        self._reconnect_attempts: dict[int, int] = {}

    def _room_lock(self, room_id: int) -> asyncio.Lock:
        """取该房间的锁（惰性创建）"""
        lock = self._room_locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            self._room_locks[room_id] = lock
        return lock

    # P0-8: 连续探测失败多少次后才认定"确实下播"（把在录场次结束）
    PROBE_FAIL_THRESHOLD = 3
    # P1-12: 同一场次断流重连最大尝试次数（超过则 finalize 并置 failed）
    RECONNECT_MAX_ATTEMPTS = 5

    def _disk_over_limit_warn(self, context: str) -> bool:
        """磁盘超过 max_disk_usage 水位时告警（10 分钟节流），返回是否超限"""
        if not file_manager.disk_over_limit():
            return False
        if time.time() - self._last_disk_warn > 600:
            self._last_disk_warn = time.time()
            msg = (f"磁盘使用率已达上限 {settings.max_disk_usage}%，暂停{context}。"
                   f"请清理 {settings.output_dir} 或在设置页调高水位后自动恢复")
            logger.warning(msg)
            task = asyncio.create_task(self._notify(msg))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        return True

    async def _get_platform(self, platform_name: str):
        """获取平台适配器实例（共享 PlatformManager，Cookie/代理变化自动重建）"""
        return await platform_manager.get(platform_name)

    async def _reset_platform_cache(self):
        """清空共享适配器缓存（修改 Cookie/代理/URL 后调用）"""
        await platform_manager.reset()

    async def start(self):
        """启动监控"""
        if self._running:
            return

        # P0-3: 启动时恢复上次异常退出（如容器被强杀）遗留的录制记录，
        # 这些记录停留在 recording 且已无 ffmpeg 进程，需标记为 failed 以免永远显示「录制中」
        await self._recover_stale_recordings()

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("直播监控调度器已启动")

    async def stop(self):
        """停止监控"""
        self._running = False

        # P1-21: 必须先停 ffmpeg，再 cancel 监控循环。
        # 原实现先 cancel _monitor_loop，若 _check_room 恰好停在 spawn 与登记/写库
        # 之间被取消，关闭后就会留下一个未被跟踪的 ffmpeg 进程继续写文件。
        # 先停录制进程可确保不会再有新的 spawn 发生（stop_recording 内部有锁）。
        #
        # P0-14 配套: 并发停止所有房间，而不是逐个 await。
        # 原实现是串行 `for ... await stop_recording()`，N 路录制时最坏 N×10s；
        # 并发后总预算约等于单个房间的停止时间（≈2×_STOP_GRACE），
        # 使 compose 的 stop_grace_period=60s 在更多并发路数下仍然够用。
        room_ids = list(recorder.active_processes.keys())
        if room_ids:
            results = await asyncio.gather(
                *(recorder.stop_recording(rid) for rid in room_ids),
                return_exceptions=True,
            )
            for rid, res in zip(room_ids, results):
                if isinstance(res, Exception):
                    logger.warning(f"停止房间 {rid} 录制进程失败: {res}")

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None

        # 取消当日合并等后台任务（若 ffmpeg concat 正在进行，给它一段完成窗口，
        # 避免强杀留下 .merge_tmp 半成品；超窗则放行关闭，由容器停止信号兜底）
        if self._bg_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._bg_tasks), return_exceptions=True),
                    timeout=8,
                )
            except asyncio.TimeoutError:
                logger.warning("后台合并任务未在 8s 内完成，跳过等待继续关闭")
            self._bg_tasks.clear()

        # 关闭共享平台适配器实例
        await platform_manager.reset()

        logger.info("直播监控调度器已停止")

    async def _monitor_loop(self):
        """监控主循环"""
        while self._running:
            try:
                await self._check_all_rooms()
            except Exception as e:
                logger.error(f"监控循环异常: {e}")

            # P1-4: 每天清理一次 30 天前的系统日志，避免 system_logs 表无限增长
            try:
                now_ts = time.time()
                if now_ts - self._last_log_cleanup > 86400:
                    self._last_log_cleanup = now_ts
                    await self._cleanup_old_logs()
            except Exception as e:
                logger.warning(f"清理旧系统日志失败: {e}")

            await asyncio.sleep(settings.monitor_interval)

    async def _refresh_loop(self):
        """流地址主动刷新循环（独立于主检测，缩短刷新粒度）。

        快手/B站/抖音的拉流地址多为带 txTime/txSecret 的短时效签名 URL，过期后
        ffmpeg 会断流。该循环在录制期间以 stream_url_refresh_interval 的粒度重新
        探测最新流地址，若发生变化则在旧地址过期前用新地址重启 ffmpeg，从而消除
        「录几分钟就断」与重连空白。设为 0 时本循环不工作（仅靠断流重连兜底）。
        """
        # 首次启动延迟一个间隔，避免与开播检测争抢
        while self._running:
            interval = settings.stream_url_refresh_interval
            if not interval or interval <= 0:
                # 关闭主动刷新：长轮询等待，直到被 stop() 取消
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._refresh_stream_urls()
            except Exception as e:
                logger.error(f"流地址刷新循环异常: {e}")

    async def _recover_stale_recordings(self):
        """启动时恢复上次异常退出遗留的录制记录（P0-3）。

        服务被强杀（如容器 SIGKILL）重启后，DB 中可能有 status=recording 但已无
        ffmpeg 进程的记录，且 mp4 文件因 moov atom 未写可能损坏。这里把它们标记为
        failed，并复位房间的 is_recording，避免前端永远显示「录制中」。
        """
        try:
            async with async_session() as session:
                res = await session.execute(
                    select(Recording).where(Recording.status == "recording")
                )
                recs = res.scalars().all()
                for rec in recs:
                    # file_path 存的是相对 output_dir 的路径（历史数据可能为绝对路径，join 时自动兼容）
                    fp = os.path.join(settings.output_dir, rec.file_path) if rec.file_path else ""
                    size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
                    await session.execute(
                        update(Recording).where(Recording.id == rec.id).values(
                            status="failed",
                            file_size=size,
                            ended_at=utcnow(),
                            error_message="服务重启恢复：检测到进行中但无录制进程，标记为失败",
                        )
                    )
                    await session.execute(
                        update(Room).where(Room.id == rec.room_id).values(is_recording=False)
                    )
                if recs:
                    await session.commit()
                    logger.warning(f"恢复 {len(recs)} 条遗留录制记录为 failed")
        except Exception as e:
            logger.error(f"恢复遗留录制记录失败: {e}")

    async def _cleanup_old_logs(self):
        """清理 30 天前的系统日志（P1-4），避免 system_logs 表无限增长。"""
        cutoff = utcnow() - timedelta(days=30)
        async with async_session() as session:
            await session.execute(delete(SystemLog).where(SystemLog.created_at < cutoff))
            await session.commit()
        logger.debug("已清理 30 天前的系统日志")

    async def _refresh_stream_urls(self):
        """对正在录制且进程存活的房间，重新探测流地址；若变化则用新地址续写同一场。"""
        if not self._room_states:
            return

        room_ids = [rid for rid, st in self._room_states.items() if st.get("recording")]
        if not room_ids:
            return

        async with async_session() as session:
            result = await session.execute(
                select(Room).where(Room.id.in_(room_ids), Room.enabled == True)
            )
            rooms = result.scalars().all()

        for room in rooms:
            # 仅当 ffmpeg 进程仍存活时才主动换地址（进程已死交给断流重连处理）
            if not await recorder.is_recording(room.id):
                continue

            platform = await self._get_platform(room.platform)
            if not platform:
                continue

            try:
                info = await platform.get_room_info(room.url)
            except Exception as e:
                logger.warning(f"刷新流地址时检测房间 {room.id} 失败: {e}")
                continue

            if not info.is_live or not info.stream_url:
                # 主播已下播或这次没拿到地址：交给主循环最终化/重连，不在此处理
                continue

            prev = self._room_states.get(room.id, {}).get("stream_url", "")
            if info.stream_url != prev:
                logger.info(
                    f"房间 {room.id} 流地址已刷新（短时效签名），用新地址续写同一场录制"
                )
                await self._reconnect_session(room, info)
                self._room_states[room.id]["stream_url"] = info.stream_url

    async def _check_all_rooms(self):
        """检查所有启用的房间"""
        async with async_session() as session:
            result = await session.execute(
                select(Room).where(Room.enabled == True)
            )
            rooms = result.scalars().all()

        if not rooms:
            return

        logger.debug(f"检查 {len(rooms)} 个房间...")

        # 并发检查所有房间
        tasks = [self._check_room(room) for room in rooms]
        await asyncio.gather(*tasks, return_exceptions=True)

        # 清理已完成的录制进程
        finished = await recorder.cleanup_finished()
        if finished:
            await self._handle_finished_recordings(finished)

    async def _check_room(self, room: Room):
        """检查单个房间状态"""
        platform = await self._get_platform(room.platform)
        if not platform:
            logger.warning(f"不支持的平台: {room.platform}")
            return

        try:
            info: RoomInfo = await platform.get_room_info(room.url)

            # P0-8: 探测失败（网络/风控/解析异常）绝不当作"下播"处理 ——
            # 适配器已用 probe_ok=False 标记，这里直接跳过本轮的**所有状态迁移**，
            # 只累加失败计数；连续 N 次失败才允许走下播路径。
            probe_ok = getattr(info, "probe_ok", True)
            if not probe_ok:
                fails = self._probe_fail_counts.get(room.id, 0) + 1
                self._probe_fail_counts[room.id] = fails
                logger.warning(
                    f"房间 {room.id} 探测失败（第 {fails} 次），本轮不做状态迁移"
                    f"（避免把网络抖动误判为下播而切断正在进行的录制）"
                )
                if fails < self.PROBE_FAIL_THRESHOLD:
                    return
                # 连续失败达到阈值：视为确认下播（否则会永远卡在录制中）
                logger.error(
                    f"房间 {room.id} 连续 {fails} 次探测失败，按已下播处理并结束当前场次"
                )
                info.is_live = False
            else:
                # 探测成功即清零失败计数
                self._probe_fail_counts.pop(room.id, None)

            # P0-7: 本房间的状态迁移串行化，避免与 check_room_now / 手动停止 /
            # _handle_finished_recordings 并发进入 _finalize_session
            async with self._room_lock(room.id):
                await self._check_room_locked(room, info)

        except Exception as e:
            logger.error(f"检查房间 {room.id} ({room.url}) 失败: {e}")

    async def _check_room_locked(self, room: Room, info: RoomInfo):
        """房间状态迁移执行体（已持该房间的锁）"""
        was_live = room.is_live
        now = utcnow()

        async with async_session() as session:
            # 更新房间状态
            update_data = {
                "is_live": info.is_live,
                "last_check_time": now,
                "title": info.title or room.title,
                # 手动填写的主播名优先（编辑弹窗承诺：清空才恢复自动探测），
                # 探测结果只回填空值
                "streamer_name": room.streamer_name or info.streamer_name,
                "room_id": info.room_id or room.room_id,
                # 主播平台用户ID：作品订阅的依据，检测到即回填（不清空已有值）
                "platform_user_id": info.owner_user_id or room.platform_user_id,
            }

            if info.is_live and not was_live:
                update_data["last_live_time"] = now
                await self._log(session, "info", "monitor",
                                f"房间 {room.streamer_name or room.url} 开播了: {info.title}")

            await session.execute(
                update(Room).where(Room.id == room.id).values(**update_data)
            )
            # P2-6: 提交后在同一 session 内重读最新 is_recording，
            # 避免循环开始时的快照值在并发刷新/重连时误判
            cur = (await session.execute(
                select(Room.is_recording).where(Room.id == room.id)
            )).first()
            await session.commit()

        room_is_recording = bool(cur[0]) if cur else False

        # 状态变化处理
        if info.is_live and not room_is_recording:
            # 开播且未在录制 - 开启一场新录制（首个 part）
            if not info.stream_url:
                logger.warning(f"房间 {room.id} 开播但未获取到流地址")
            elif self._disk_over_limit_warn("新开播录制"):
                # 磁盘超水位：不再开新场次，等用户清理/调高阈值（下播最终化不受影响）
                pass
            else:
                await self._start_recording(room, info)
                room.is_recording = True

        elif not info.is_live and room_is_recording:
            # 下播 - 结束当前场次（合并所有 part 为单个文件）
            await self._finalize_session(room)
            room.is_recording = False
            self._reconnect_attempts.pop(room.id, None)

        # 断流重连检查：录制中但 ffmpeg 进程已退出（直播仍在进行）
        if room_is_recording:
            is_still_recording = await recorder.is_recording(room.id)
            if not is_still_recording:
                if info.is_live and info.stream_url:
                    # P1-12: 重连次数上限 + 磁盘水位检查，避免重启风暴
                    attempts = self._reconnect_attempts.get(room.id, 0)
                    if attempts >= self.RECONNECT_MAX_ATTEMPTS:
                        logger.error(
                            f"房间 {room.id} 断流重连已达 {attempts} 次上限，结束场次并标记失败"
                        )
                        await self._finalize_session(room)
                        room.is_recording = False
                        self._reconnect_attempts.pop(room.id, None)
                    elif self._disk_over_limit_warn("断流重连"):
                        logger.warning(f"房间 {room.id} 磁盘超限，暂不重连")
                    else:
                        # 断流但仍在直播 -> 续写同一场录制（追加新 part，不新建记录）
                        self._reconnect_attempts[room.id] = attempts + 1
                        logger.info(
                            f"房间 {room.id} 录制进程退出，断流重连续写同一场录制"
                            f"（第 {attempts + 1}/{self.RECONNECT_MAX_ATTEMPTS} 次）"
                        )
                        await self._reconnect_session(room, info)
                else:
                    # 进程退出且已下播 -> 结束场次
                    await self._finalize_session(room)
                    room.is_recording = False
                    self._reconnect_attempts.pop(room.id, None)

    async def _start_recording(self, room: Room, info: RoomInfo):
        """开播/手动开始：开启一场新录制（一条 Recording + 首个 part）"""
        # 文件格式只由全局 record_format 决定；room.quality 是画质语义，
        # 绝不能当格式用（曾导致选"蓝光"生成 .blue_ray 文件 ffmpeg 直接失败）
        fmt = settings.record_format
        # 主播名优先级：手动填写 > 平台探测 > 备注 > 房间ID。
        # 平台游客态常拿不到主播名，若不回退备注，文件名/目录会退化为纯房间号。
        streamer = room.streamer_name or info.streamer_name or room.remark or room.room_id
        # 归档目录名在这里敲定：平台探测到的主播名只有这个时刻拿得到
        async with async_session() as session:
            row = await session.get(Room, room.id)
            if row:
                await archive.sync_folder_name(session, row, display=streamer)
                room.folder_name = row.folder_name
        final_path, part_target = recorder.build_session_target(
            room.platform,
            streamer,
            room.room_id,
            base_dir=archive.live_root(room),
            part_index=1,
            record_format=fmt,
            segment_time=settings.segment_time,
            template=settings.filename_template,
            title=info.title,
            remark=room.remark,
        )

        result = await recorder.start_recording(
            room_db_id=room.id,
            stream_url=info.stream_url,
            platform=room.platform,
            streamer_name=streamer,
            room_id=info.room_id or room.room_id,
            record_format=fmt,
            output_path=part_target,
            segment_time=settings.segment_time,
        )

        if result["success"]:
            async with async_session() as session:
                # 创建录制记录：file_path 存相对 output_dir 的最终文件路径，
                # part_paths 记录首个分片（相对路径）
                recording = Recording(
                    room_id=room.id,
                    file_path=self._rel(final_path),
                    file_name=os.path.basename(final_path),
                    format=fmt,
                    status="recording",
                    started_at=utcnow(),
                    part_paths=json.dumps([self._rel(result["file_path"])]),
                )
                session.add(recording)
                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=True)
                )
                await session.commit()

            # 记录当前使用的流地址，供主动刷新循环判断是否需要换新地址续写
            self._room_states[room.id] = {
                "recording": True,
                "stream_url": info.stream_url,
            }

            logger.info(f"房间 {room.id} 开始录制: 最终文件={final_path} part={result['file_path']}")
            await self._notify(f"开始录制: {info.streamer_name} - {info.title}")

    async def _reconnect_session(self, room: Room, info: RoomInfo):
        """断流重连：续写同一场录制 —— 追加一个新 part，不新建 Recording。"""
        async with self._reconnect_lock:
            await self._reconnect_session_inner(room, info)

    async def _reconnect_session_inner(self, room: Room, info: RoomInfo):
        """断流重连真实执行体（已在外层加锁串行化）。"""
        # P1-1: 幂等检查。若本次流地址与已跟踪的一致且进程仍存活，
        # 说明刚被刷新循环/重连的另一路处理过，跳过重复 stop+start，避免视频出现空白段。
        st = self._room_states.get(room.id, {})
        if st.get("stream_url") == info.stream_url and await recorder.is_recording(room.id):
            logger.debug(f"房间 {room.id} 流地址未变化且仍在录制，跳过重复重连")
            return

        async with async_session() as session:
            result = await session.execute(
                select(Recording).where(
                    Recording.room_id == room.id,
                    Recording.status == "recording",
                ).order_by(Recording.id.desc()).limit(1)
            )
            recording = result.scalar_one_or_none()
            if not recording:
                # 异常：没有进行中的场次，退化为新开一场
                logger.warning(f"房间 {room.id} 重连时无进行中场次，改为新开录制")
                await self._start_recording(room, info)
                return

            parts = json.loads(recording.part_paths or "[]")
            next_index = len(parts) + 1
            fmt = recording.format

            # 由最终文件路径反推 base 与目录，生成下一个 part 目标
            # （DB 存相对路径，历史数据可能为绝对路径，join 对两者都正确）
            final_path = os.path.join(settings.output_dir, recording.file_path or "")
            dir_path = os.path.dirname(final_path)
            base = os.path.basename(final_path)
            if "." in base:
                base = base[: base.rfind(".")]
            seg = settings.segment_time
            if seg and seg > 0 and fmt in ("ts", "mp4"):
                part_target = os.path.join(dir_path, f"{base}_part{next_index:03d}_%04d.{fmt}")
            else:
                part_target = os.path.join(dir_path, f"{base}_part{next_index:03d}.{fmt}")

            # 确保旧的（已退出）ffmpeg 进程被回收
            await recorder.stop_recording(room.id)

            rec = await recorder.start_recording(
                room_db_id=room.id,
                stream_url=info.stream_url,
                platform=room.platform,
                streamer_name=room.streamer_name or info.streamer_name or room.remark or room.room_id,
                room_id=info.room_id or room.room_id,
                record_format=fmt,
                output_path=part_target,
                segment_time=seg,
            )
            if rec["success"]:
                parts.append(self._rel(rec["file_path"]))
                await session.execute(
                    update(Recording).where(Recording.id == recording.id).values(
                        part_paths=json.dumps(parts)
                    )
                )
                await session.commit()
                # 同步跟踪的最新流地址，供主动刷新循环判断后续是否再次变化
                st = self._room_states.setdefault(room.id, {"recording": True})
                st["recording"] = True
                st["stream_url"] = info.stream_url
                logger.info(f"房间 {room.id} 断流重连续写同一场录制 (part {next_index}: {rec['file_path']})")
                await self._notify(f"断流重连，继续录制: {info.streamer_name}")
            else:
                logger.error(f"房间 {room.id} 重连失败: {rec.get('error')}")

    async def _finalize_session(self, room: Room, recording: Recording = None):
        """停止一场录制：结束 ffmpeg，把所有 part 合并成最终单个文件。

        P0-7: 由调用方持有该房间的锁（_check_room_locked / _handle_finished_recordings /
        手动停止入口）。这里再用 CAS 兜底：只有 `status='recording'` 的行才会被收尾，
        并发第二次进入时 rowcount=0 直接返回，避免两路 concat 同一路径。
        """
        # 结束底层 ffmpeg 进程（已退出则安全返回）
        await recorder.stop_recording(room.id)

        # 清除该房间的录制态跟踪，主动刷新循环不再对其刷新
        self._room_states.pop(room.id, None)

        if recording is None:
            async with async_session() as session:
                res = await session.execute(
                    select(Recording).where(
                        Recording.room_id == room.id,
                        Recording.status == "recording",
                    ).order_by(Recording.id.desc()).limit(1)
                )
                recording = res.scalar_one_or_none()

        if recording is None:
            async with async_session() as session:
                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=False)
                )
                await session.commit()
            return

        final_path = os.path.join(settings.output_dir, recording.file_path or "")

        # P1-13: part_paths 是历史/损坏 JSON 时不能让 ValueError 冒泡 —— 原实现异常被
        # 外层吞掉后行仍停在 status=recording，每轮都重试 finalize，会话永不关闭。
        parts_rel = self._parse_parts(recording.part_paths)
        files = self._expand_parts(parts_rel)

        merge_error = None
        merged_ok = True

        if not files:
            logger.warning(f"房间 {room.id} 没有可合并的录制文件")
            merged_ok = False
            merge_error = "没有可合并的录制文件（part 全部缺失）"
        elif len(files) == 1:
            # 单 part：若不是最终路径则移过去
            if os.path.abspath(files[0]) != os.path.abspath(final_path):
                try:
                    os.replace(files[0], final_path)
                except OSError as e:
                    logger.error(f"移动单段文件失败: {e}")
                    merged_ok = False
                    merge_error = f"移动单段文件失败: {e}"
        else:
            # 多 part：合并为单个最终文件后删除碎片（P0-1：合并目标用原计划路径，
            # 保持在「主播/直播间/日期」目录内与命名，避免落到无主目录导致无法追溯）
            #
            # P0-4: 原实现直接同步调用 merge_recordings —— 其内部是
            # subprocess.run(timeout=1800)，在事件循环里跑会把全站 API、监控循环、
            # 作品下载全部阻塞最长 30 分钟。必须放线程池（与 _daily_merge_inner 一致）。
            #
            # P1-15: concat 必须与 _daily_merge 共用 _merge_lock —— 上一场的当日合并
            # 正在删除旧分文件时，若本场 finalize 恰好把其中某个当作 concat 输入，
            # 会得到"合并失败/半成品"。所有 ffmpeg concat 串行化。
            async with self._merge_lock:
                merged = await asyncio.to_thread(
                    file_manager.merge_recordings,
                    [os.path.relpath(f, settings.output_dir) for f in files],
                    recording.format,
                    final_path,
                )
            if merged.get("success"):
                final_path = merged["output_path"]
                for f in files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                self._clean_empty_dirs(os.path.dirname(merged["output_path"]))
            else:
                merged_ok = False
                merge_error = merged.get("error") or "ffmpeg 合并失败"
                logger.error(f"房间 {room.id} 合并碎片失败: {merge_error}")

        size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
        now = utcnow()
        duration = (now - recording.started_at).total_seconds() if recording.started_at else 0

        # P0-1: 只有"确实产出了非空最终文件"才算 completed。
        # 原实现无论合并成败都写 status=completed + part_paths=None + file_size=0，
        # 于是碎片文件仍在磁盘却**再无任何 DB 记录指向它们**（_expand_parts 只在
        # finalize 内部用）—— 这些文件再也无法被合并/同步/列出，属不可恢复的数据丢失。
        # 失败时保留 part_paths 供后续重试合并，并如实标记 failed + 原因。
        if merged_ok and size > 0:
            new_values = {
                "status": "completed",
                "file_path": self._rel(final_path),
                "file_name": os.path.basename(final_path),
                "file_size": size,
                "duration": duration,
                "ended_at": now,
                "part_paths": None,
                "error_message": None,
            }
        else:
            tail = recorder.last_stderr(room.id)
            reason = merge_error or "最终文件为空"
            if tail:
                reason = f"{reason} | ffmpeg 尾部输出: {tail[-300:]}"
            new_values = {
                "status": "failed",
                "file_path": self._rel(final_path),
                "file_name": os.path.basename(final_path),
                "file_size": size,
                "duration": duration,
                "ended_at": now,
                # 关键：保留碎片引用，使它们仍可被列出/同步/重试合并
                "part_paths": json.dumps(parts_rel) if parts_rel else None,
                "error_message": reason[:500],
            }
            logger.error(f"房间 {room.id} 场次收尾失败，保留 {len(parts_rel)} 个 part 引用: {reason}")

        async with async_session() as session:
            # P0-7 CAS：仅当该行仍是 recording 时才收尾，并发第二路 rowcount=0 直接跳过
            res = await session.execute(
                update(Recording).where(
                    Recording.id == recording.id,
                    Recording.status == "recording",
                ).values(**new_values)
            )
            if res.rowcount == 0:
                logger.warning(
                    f"房间 {room.id} 场次 {recording.id} 已被其他路径收尾，跳过重复 finalize"
                )
                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=False)
                )
                await session.commit()
                return
            await session.execute(
                update(Room).where(Room.id == room.id).values(is_recording=False)
            )
            await session.commit()

        if new_values["status"] == "completed":
            logger.info(f"房间 {room.id} 录制结束，最终文件: {final_path}")
            await self._notify(f"录制结束: {room.streamer_name or room.url}")

            # 当日跨场次自动合并（后台执行，不阻塞监控循环）
            fmt = recording.format or settings.record_format
            task = asyncio.create_task(self._daily_merge(room.id, os.path.dirname(final_path), fmt))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        else:
            await self._notify(
                f"录制收尾失败: {room.streamer_name or room.url}（碎片已保留，可在文件管理中手动合并）"
            )

    async def _daily_merge(self, room_id: int, dir_path: str, fmt: str):
        """当日跨场次合并：同目录下该主播多段已完成录制合并为一个文件。

        条件：≥2 个文件且总大小 ≤ daily_merge_max_gb（GB，0 关闭）。
        ffmpeg concat 流拷贝不重编码；合并后录制记录收敛为一条（保留最早一条，
        更新为合并产物），多余记录删除，旧分文件从磁盘移除。
        """
        if not settings.daily_merge_max_gb or settings.daily_merge_max_gb <= 0:
            return
        async with self._merge_lock:
            try:
                await self._daily_merge_inner(room_id, dir_path, fmt)
            except Exception as e:
                logger.error(f"当日合并失败 房间{room_id} {dir_path}: {e}")

    async def _daily_merge_inner(self, room_id: int, dir_path: str, fmt: str):
        async with async_session() as session:
            rows = (await session.execute(
                select(Recording).where(
                    Recording.room_id == room_id,
                    Recording.status == "completed",
                ).order_by(Recording.started_at)
            )).scalars().all()
            folder = await session.scalar(select(Room.folder_name).where(Room.id == room_id))

        # file_path 统一按相对路径解读（历史绝对路径数据 join 后原样保留），
        # 再用绝对路径与 dir_path 比对——此前直接比较导致已合并记录（相对路径）
        # 匹配不上，当天第三次及以后的开播不再参与二次合并
        target_dir = os.path.abspath(dir_path)

        def _abs(fp: str) -> str:
            return os.path.abspath(os.path.join(settings.output_dir, fp))

        day = [r for r in rows
               if r.file_path and os.path.dirname(_abs(r.file_path)) == target_dir]
        if len(day) < 2:
            return
        files = [_abs(r.file_path) for r in day]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) < 2:
            return

        total = sum(os.path.getsize(f) for f in files)
        max_bytes = settings.daily_merge_max_gb * 1024 ** 3
        if total > max_bytes:
            logger.info(
                f"房间 {room_id} 当日录制共 {round(total / 1024 ** 3, 2)}GB "
                f"超过上限 {settings.daily_merge_max_gb}GB，跳过当日合并"
            )
            return

        # 命名取归档目录名与日期目录：{主播}_{YYYYMMDD}_合并.{fmt}
        # 主播名取自 Room.folder_name 而非目录层级——新布局是 主播/直播间/日期，
        # 上一级目录恒为「直播间」，用路径反推会把合并文件叫「直播间_…」
        streamer_dir = folder or os.path.basename(os.path.dirname(dir_path))
        date_compact = os.path.basename(dir_path).replace("-", "")
        final_name = f"{streamer_dir}_{date_compact}_合并.{fmt}"
        # 隐藏临时产物（点前缀不会出现在文件列表），成功后原子改名；同名旧合并文件
        # 作为输入之一会在改名时被安全覆盖（ffmpeg 已先完成读取）。
        # 注意 dir_path 是相对 output_dir 的路径，磁盘操作必须转绝对路径
        tmp_out = os.path.join(settings.output_dir, dir_path, f".merge_tmp_{date_compact}.{fmt}")
        final_abs = os.path.join(settings.output_dir, dir_path, final_name)

        merged = await asyncio.to_thread(
            file_manager.merge_recordings,
            [os.path.relpath(f, settings.output_dir) for f in files],
            output_format=fmt,
            output_path=tmp_out,
        )
        if not merged.get("success"):
            logger.error(f"房间 {room_id} 当日合并失败: {merged.get('error')}")
            try:
                os.remove(tmp_out)
            except OSError:
                pass
            return
        os.replace(tmp_out, final_abs)

        size = os.path.getsize(final_abs)
        now = utcnow()
        keep = day[0]
        duration_sum = sum(r.duration or 0 for r in day)
        ended = max([r.ended_at for r in day if r.ended_at], default=now)
        async with async_session() as session:
            await session.execute(
                update(Recording).where(Recording.id == keep.id).values(
                    file_path=os.path.relpath(final_abs, settings.output_dir),
                    file_name=final_name,
                    file_size=size,
                    duration=duration_sum,
                    ended_at=ended,
                    part_paths=None,
                )
            )
            for r in day[1:]:
                await session.execute(delete(Recording).where(Recording.id == r.id))
            await session.commit()

        # 删除旧分文件；合并产物本身可能也是本轮输入（当天二次合并），必须保留
        for f in files:
            if os.path.abspath(f) == final_abs:
                continue
            try:
                os.remove(f)
            except OSError:
                pass
        file_manager.invalidate_cache("file_list")
        logger.info(
            f"房间 {room_id} 当日合并完成: {final_name} "
            f"({round(size / 1024 ** 3, 2)}GB, {len(files)} 段)"
        )
        await self._notify(f"当日合并完成: {streamer_dir} {final_name}")

    # ---------------------------------------------------------------- 公开 API
    # P1-30: HTTP 层不应直接调用服务的私有方法。原 routers/rooms.py 与
    # routers/system.py 直接调 monitor._get_platform / _start_recording /
    # _stop_recording / _reset_platform_cache，服务边界形同虚设 ——
    # 改 monitor 内部实现会直接破坏 API 行为且无编译期提示。
    # 这里提供语义化的公开方法，私有实现保持不变。

    async def get_platform(self, platform_name: str):
        """获取平台适配器实例（供 API 层使用）"""
        return await self._get_platform(platform_name)

    async def reset_platform_cache(self):
        """清空平台适配器缓存（修改 Cookie/代理/URL 后调用）"""
        await self._reset_platform_cache()

    async def start_recording(self, room: Room, info: RoomInfo):
        """手动开始一场录制（供 API 层使用）"""
        await self._start_recording(room, info)

    async def stop_recording(self, room: Room):
        """停止并最终化当前场次（供 API 层使用）

        与监控循环共用同一把 per-room 锁，避免 API 手动停止与循环判定下播并发 finalize。
        """
        async with self._room_lock(room.id):
            await self._finalize_session(room)

    async def _stop_recording(self, room: Room, update_status: bool = True):
        """[已弃用] 旧私有别名，保留仅为兼容；新代码请用 stop_recording()。"""
        await self._finalize_session(room)

    def _rel(self, path: str) -> str:
        """转为相对 output_dir 的路径用于存储"""
        return os.path.relpath(path, settings.output_dir)

    @staticmethod
    def _parse_parts(raw: str) -> list:
        """安全解析 part_paths JSON（P1-13）。

        历史数据可能为空串/损坏 JSON；原实现直接 json.loads，ValueError 被外层吞掉后
        该行仍停在 status=recording，每轮都重试 finalize，会话永不关闭。
        """
        if not raw:
            return []
        try:
            parts = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(f"part_paths 不是合法 JSON，按空处理: {raw[:120]}")
            return []
        if not isinstance(parts, list):
            return []
        return [p for p in parts if isinstance(p, str) and p]

    def _expand_parts(self, parts_rel: list) -> list:
        """把存储的 part 路径展开为实际文件列表（处理 segment 的 %04d 通配）"""
        import glob
        files = []
        for p in parts_rel:
            full = os.path.join(settings.output_dir, p)
            if "%04d" in full:
                matches = sorted(glob.glob(full.replace("%04d", "*")))
                files.extend(matches)
            elif os.path.exists(full):
                files.append(full)
        return files

    def _clean_empty_dirs(self, path: str):
        """向上清理空目录（仅清理 output_dir 内的日期/主播目录）

        P1-22: 前缀必须带分隔符比较。原写法 `parent.startswith(base)` 在
        output_dir 为兄弟路径前缀时（/app/recordings 与 /app/recordings_evil）
        会误判并可能向上 rmdir 到 output_dir 之外。
        """
        base = os.path.abspath(settings.output_dir)
        prefix = base.rstrip(os.sep) + os.sep
        parent = os.path.abspath(path)
        while parent and parent != base and parent.startswith(prefix):
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
                else:
                    break
            except OSError:
                break

    async def _handle_finished_recordings(self, finished_room_ids: list):
        """处理已完成的录制进程（安全网）。

        正常情况下断流重连 / 下播已在 _check_room 内处理；这里仅兜底处理
        「进程已退出但 _check_room 尚未轮询到」的情况：仍直播则交由重连逻辑，
        已下播则最终化。避免与重连逻辑重复最终化导致碎片。
        """
        for room_id in finished_room_ids:
            async with async_session() as session:
                result = await session.execute(select(Room).where(Room.id == room_id))
                room = result.scalar_one_or_none()
                if not room:
                    continue
                if room.is_live:
                    # 仍直播中，断流重连由 _check_room 处理，避免重复最终化
                    continue
                rec_res = await session.execute(
                    select(Recording).where(
                        Recording.room_id == room_id,
                        Recording.status == "recording"
                    ).order_by(Recording.id.desc()).limit(1)
                )
                recording = rec_res.scalar_one_or_none()
                if recording:
                    # P0-7: 与 _check_room_locked 共用同一把 per-room 锁，
                    # 避免"监控循环正在 finalize"与"这里兜底 finalize"并发执行
                    async with self._room_lock(room_id):
                        await self._finalize_session(room, recording)

    async def _notify(self, message: str):
        """发送通知"""
        if not settings.enable_notification:
            return

        try:
            async with httpx.AsyncClient() as client:
                await client.post(settings.webhook_url, json={"text": message}, timeout=10)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    async def _log(self, session, level: str, module: str, message: str):
        """记录系统日志"""
        log = SystemLog(level=level, module=module, message=message)
        session.add(log)

    async def check_room_now(self, room_id: int):
        """手动触发检查单个房间"""
        async with async_session() as session:
            result = await session.execute(select(Room).where(Room.id == room_id))
            room = result.scalar_one_or_none()

        if room:
            await self._check_room(room)


# 全局监控器实例
monitor = LiveMonitor()
