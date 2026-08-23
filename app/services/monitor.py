"""直播监控调度器 - 定时检测房间状态并自动录制"""
import asyncio
import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, update, delete
from app.database import async_session
from app.models import Room, Recording, SystemLog
from app.config import settings
from app.services.recorder import recorder
from app.services.file_manager import file_manager
from app.services.platform import PlatformFactory, RoomInfo

logger = logging.getLogger(__name__)


class LiveMonitor:
    """直播监控调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._platform_instances: dict = {}
        self._room_states: dict[int, dict] = {}
        # 串行化所有重连/换地址操作，避免刷新循环与断流重连同时对同一房间重启 ffmpeg
        self._reconnect_lock = asyncio.Lock()
        # 上次清理旧系统日志的时间戳（P1-4，按天节流）
        self._last_log_cleanup: float = 0.0

    async def _get_platform(self, platform_name: str):
        """获取平台适配器实例（cookie/proxy 变化则自动重建，避免缓存到过期空Cookie）"""
        cookie = ""
        if platform_name == "douyin":
            cookie = settings.douyin_cookie
        elif platform_name == "bilibili":
            cookie = settings.bilibili_cookie
        elif platform_name == "kuaishou":
            cookie = settings.kuaishou_cookie
        proxy = settings.proxy_addr if settings.enable_proxy else ""

        cached = self._platform_instances.get(platform_name)
        # cookie 或 proxy 变化（或首次）→ 重建实例，使最新的 Cookie 立即生效
        if cached is not None and getattr(cached, "cookie", None) == cookie \
                and getattr(cached, "proxy", None) == proxy:
            return cached

        # P0-2: 重建前先关闭旧实例，避免 httpx.AsyncClient 连接池泄漏
        if cached is not None:
            try:
                await cached.close()
            except Exception:
                pass

        instance = PlatformFactory.get_platform(
            platform_name,
            proxy=proxy,
            cookie=cookie,
            timeout=settings.check_timeout,
        )
        if instance:
            self._platform_instances[platform_name] = instance

        return self._platform_instances.get(platform_name)

    async def _reset_platform_cache(self):
        """关闭并清空所有平台适配器实例（修改 cookie/proxy/URL 后调用）。

        统一在此 close 旧实例的 httpx.AsyncClient，避免连接池泄漏（P0-2）。
        替代直接操作 ``_platform_instances.clear()`` 的调用点。
        """
        for inst in list(self._platform_instances.values()):
            try:
                await inst.close()
            except Exception:
                pass
        self._platform_instances.clear()

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

        # P0-3: 优雅停止所有进行中的 ffmpeg，让 mp4 正常写 moov atom，避免文件损坏
        for room_id in list(recorder.active_processes.keys()):
            try:
                await recorder.stop_recording(room_id)
            except Exception as e:
                logger.warning(f"停止房间 {room_id} 录制进程失败: {e}")

        # 关闭所有平台适配器实例
        for platform_instance in list(self._platform_instances.values()):
            try:
                await platform_instance.close()
            except Exception:
                pass
        self._platform_instances.clear()

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
                    fp = rec.file_path or ""
                    size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
                    await session.execute(
                        update(Recording).where(Recording.id == rec.id).values(
                            status="failed",
                            file_size=size,
                            ended_at=datetime.now(),
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
        cutoff = datetime.now() - timedelta(days=30)
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

            was_live = room.is_live
            now = datetime.utcnow()

            async with async_session() as session:
                # 更新房间状态
                update_data = {
                    "is_live": info.is_live,
                    "last_check_time": now,
                    "title": info.title or room.title,
                    "streamer_name": info.streamer_name or room.streamer_name,
                    "room_id": info.room_id or room.room_id,
                }

                if info.is_live and not was_live:
                    update_data["last_live_time"] = now
                    await self._log(session, "info", "monitor",
                                    f"房间 {room.streamer_name or room.url} 开播了: {info.title}")

                await session.execute(
                    update(Room).where(Room.id == room.id).values(**update_data)
                )
                await session.commit()

            # P2-6: 重新读取最新 is_recording，避免循环开始时的快照值在并发刷新/重连时误判
            cur = (await session.execute(
                select(Room.is_recording).where(Room.id == room.id)
            )).first()
            room_is_recording = bool(cur[0]) if cur else False

            # 状态变化处理
            if info.is_live and not room_is_recording:
                # 开播且未在录制 - 开启一场新录制（首个 part）
                if info.stream_url:
                    await self._start_recording(room, info)
                    room.is_recording = True
                else:
                    logger.warning(f"房间 {room.id} 开播但未获取到流地址")

            elif not info.is_live and room_is_recording:
                # 下播 - 结束当前场次（合并所有 part 为单个文件）
                await self._finalize_session(room)
                room.is_recording = False

            # 断流重连检查：录制中但 ffmpeg 进程已退出（直播仍在进行）
            if room_is_recording:
                is_still_recording = await recorder.is_recording(room.id)
                if not is_still_recording:
                    if info.is_live and info.stream_url:
                        # 断流但仍在直播 -> 续写同一场录制（追加新 part，不新建记录）
                        logger.info(f"房间 {room.id} 录制进程退出，断流重连续写同一场录制")
                        await self._reconnect_session(room, info)
                    else:
                        # 进程退出且已下播 -> 结束场次
                        await self._finalize_session(room)
                        room.is_recording = False

        except Exception as e:
            logger.error(f"检查房间 {room.id} ({room.url}) 失败: {e}")

    async def _start_recording(self, room: Room, info: RoomInfo):
        """开播/手动开始：开启一场新录制（一条 Recording + 首个 part）"""
        fmt = room.quality if room.quality and room.quality != "origin" else settings.record_format
        final_path, part_target = recorder.build_session_target(
            room.platform,
            info.streamer_name or room.streamer_name or room.room_id,
            room.room_id,
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
            streamer_name=info.streamer_name or room.streamer_name or room.room_id,
            room_id=info.room_id or room.room_id,
            record_format=fmt,
            output_path=part_target,
            segment_time=settings.segment_time,
        )

        if result["success"]:
            async with async_session() as session:
                # 创建录制记录：file_path 指向最终文件，part_paths 记录首个分片
                recording = Recording(
                    room_id=room.id,
                    file_path=final_path,
                    file_name=os.path.basename(final_path),
                    format=fmt,
                    status="recording",
                    started_at=datetime.now(),
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
            final_path = recording.file_path
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
                streamer_name=info.streamer_name or room.streamer_name or room.room_id,
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
        """停止一场录制：结束 ffmpeg，把所有 part 合并成最终单个文件。"""
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

        final_path = recording.file_path
        parts_rel = json.loads(recording.part_paths or "[]") or []
        files = self._expand_parts(parts_rel)

        if not files:
            logger.warning(f"房间 {room.id} 没有可合并的录制文件")
        elif len(files) == 1:
            # 单 part：若不是最终路径则移过去
            if os.path.abspath(files[0]) != os.path.abspath(final_path):
                try:
                    os.replace(files[0], final_path)
                except OSError as e:
                    logger.error(f"移动单段文件失败: {e}")
        else:
            # 多 part：合并为单个最终文件后删除碎片（P0-1：合并目标用原计划路径，
            # 保持「平台/主播/日期」目录结构与命名，避免落到 merged/ 导致无法追溯）
            merged = file_manager.merge_recordings(
                [os.path.relpath(f, settings.output_dir) for f in files],
                output_format=recording.format,
                output_path=final_path,
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
                logger.error(f"房间 {room.id} 合并碎片失败: {merged.get('error')}")

        size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
        now = datetime.now()
        duration = (now - recording.started_at).total_seconds() if recording.started_at else 0

        async with async_session() as session:
            await session.execute(
                update(Recording).where(Recording.id == recording.id).values(
                    status="completed",
                    file_path=final_path,
                    file_name=os.path.basename(final_path),
                    file_size=size,
                    duration=duration,
                    ended_at=now,
                    part_paths=None,
                )
            )
            await session.execute(
                update(Room).where(Room.id == room.id).values(is_recording=False)
            )
            await session.commit()

        logger.info(f"房间 {room.id} 录制结束，最终文件: {final_path}")
        await self._notify(f"录制结束: {room.streamer_name or room.url}")

    async def _stop_recording(self, room: Room, update_status: bool = True):
        """停止录制（向下兼容 router 调用）：最终化当前场次。"""
        await self._finalize_session(room)

    def _rel(self, path: str) -> str:
        """转为相对 output_dir 的路径用于存储"""
        return os.path.relpath(path, settings.output_dir)

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
        """向上清理空目录（仅清理 output_dir 内的日期/主播目录）"""
        base = os.path.abspath(settings.output_dir)
        parent = os.path.abspath(path)
        while parent and parent.startswith(base) and parent != base:
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
                    await self._finalize_session(room, recording)

    async def _notify(self, message: str):
        """发送通知"""
        if not settings.enable_notification:
            return

        try:
            import httpx
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
