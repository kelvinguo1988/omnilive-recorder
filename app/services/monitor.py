"""直播监控调度器 - 定时检测房间状态并自动录制"""
import asyncio
import os
import json
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import select, update
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
        self._running = False
        self._platform_instances: dict = {}
        self._room_states: dict[int, dict] = {}

    def _get_platform(self, platform_name: str):
        """获取平台适配器实例"""
        if platform_name not in self._platform_instances:
            cookie = ""
            if platform_name == "douyin":
                cookie = settings.douyin_cookie
            proxy = settings.proxy_addr if settings.enable_proxy else ""

            instance = PlatformFactory.get_platform(
                platform_name,
                proxy=proxy,
                cookie=cookie,
                timeout=settings.check_timeout,
            )
            if instance:
                self._platform_instances[platform_name] = instance

        return self._platform_instances.get(platform_name)

    async def start(self):
        """启动监控"""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
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

        # 停止所有录制
        for platform_instance in self._platform_instances.values():
            await platform_instance.close()
        self._platform_instances.clear()

        logger.info("直播监控调度器已停止")

    async def _monitor_loop(self):
        """监控主循环"""
        while self._running:
            try:
                await self._check_all_rooms()
            except Exception as e:
                logger.error(f"监控循环异常: {e}")

            await asyncio.sleep(settings.monitor_interval)

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
        platform = self._get_platform(room.platform)
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

            # 状态变化处理
            if info.is_live and not room.is_recording:
                # 开播且未在录制 - 开启一场新录制（首个 part）
                if info.stream_url:
                    await self._start_recording(room, info)
                    room.is_recording = True
                else:
                    logger.warning(f"房间 {room.id} 开播但未获取到流地址")

            elif not info.is_live and room.is_recording:
                # 下播 - 结束当前场次（合并所有 part 为单个文件）
                await self._finalize_session(room)
                room.is_recording = False

            # 断流重连检查：录制中但 ffmpeg 进程已退出（直播仍在进行）
            if room.is_recording:
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
                    started_at=datetime.utcnow(),
                    part_paths=json.dumps([self._rel(result["file_path"])]),
                )
                session.add(recording)
                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=True)
                )
                await session.commit()

            logger.info(f"房间 {room.id} 开始录制: 最终文件={final_path} part={result['file_path']}")
            await self._notify(f"开始录制: {info.streamer_name} - {info.title}")

    async def _reconnect_session(self, room: Room, info: RoomInfo):
        """断流重连：续写同一场录制 —— 追加一个新 part，不新建 Recording。"""
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
                logger.info(f"房间 {room.id} 断流重连续写同一场录制 (part {next_index}: {rec['file_path']})")
                await self._notify(f"断流重连，继续录制: {info.streamer_name}")
            else:
                logger.error(f"房间 {room.id} 重连失败: {rec.get('error')}")

    async def _finalize_session(self, room: Room, recording: Recording = None):
        """停止一场录制：结束 ffmpeg，把所有 part 合并成最终单个文件。"""
        # 结束底层 ffmpeg 进程（已退出则安全返回）
        await recorder.stop_recording(room.id)

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
            # 多 part：合并为单个最终文件后删除碎片
            merged = file_manager.merge_recordings(
                [os.path.relpath(f, settings.output_dir) for f in files],
                output_format=recording.format,
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
        now = datetime.utcnow()
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
