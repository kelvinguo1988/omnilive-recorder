"""直播监控调度器 - 定时检测房间状态并自动录制"""
import asyncio
import os
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import select, update
from app.database import async_session
from app.models import Room, Recording, SystemLog
from app.config import settings
from app.services.recorder import recorder
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
                # 开播且未在录制 - 开始录制
                if info.stream_url:
                    await self._start_recording(room, info)
                else:
                    logger.warning(f"房间 {room.id} 开播但未获取到流地址")

            elif not info.is_live and room.is_recording:
                # 下播但还在录制 - 停止录制
                await self._stop_recording(room)

            # 检查录制进程是否意外退出
            if room.is_recording:
                is_still_recording = await recorder.is_recording(room.id)
                if not is_still_recording:
                    # 录制进程意外退出，检查是否还在直播
                    if info.is_live and info.stream_url:
                        logger.info(f"房间 {room.id} 录制进程退出，重新开始录制")
                        await self._stop_recording(room, update_status=False)
                        await self._start_recording(room, info)
                    else:
                        await self._stop_recording(room)

        except Exception as e:
            logger.error(f"检查房间 {room.id} ({room.url}) 失败: {e}")

    async def _start_recording(self, room: Room, info: RoomInfo):
        """开始录制"""
        result = await recorder.start_recording(
            room_db_id=room.id,
            stream_url=info.stream_url,
            platform=room.platform,
            streamer_name=info.streamer_name or room.streamer_name or room.room_id,
            room_id=info.room_id or room.room_id,
            record_format=room.quality if room.quality and room.quality != "origin" else settings.record_format,
        )

        if result["success"]:
            async with async_session() as session:
                # 创建录制记录
                recording = Recording(
                    room_id=room.id,
                    file_path=result["file_path"],
                    file_name=os.path.basename(result["file_path"]),
                    format=settings.record_format,
                    status="recording",
                    started_at=datetime.utcnow(),
                )
                session.add(recording)
                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=True)
                )
                await session.commit()

            logger.info(f"房间 {room.id} 开始录制: {result['file_path']}")
            await self._notify(f"开始录制: {info.streamer_name} - {info.title}")

    async def _stop_recording(self, room: Room, update_status: bool = True):
        """停止录制"""
        success = await recorder.stop_recording(room.id)

        if update_status or success:
            async with async_session() as session:
                # 更新录制记录
                result = await session.execute(
                    select(Recording).where(
                        Recording.room_id == room.id,
                        Recording.status == "recording"
                    ).order_by(Recording.id.desc()).limit(1)
                )
                recording = result.scalar_one_or_none()

                if recording:
                    file_size = await recorder.get_file_size(recording.file_path or "")
                    now = datetime.utcnow()
                    duration = (now - recording.started_at).total_seconds() if recording.started_at else 0

                    await session.execute(
                        update(Recording).where(Recording.id == recording.id).values(
                            status="completed",
                            file_size=file_size,
                            duration=duration,
                            ended_at=now,
                        )
                    )

                await session.execute(
                    update(Room).where(Room.id == room.id).values(is_recording=False)
                )
                await session.commit()

            logger.info(f"房间 {room.id} 录制已停止")
            await self._notify(f"录制结束: {room.streamer_name or room.url}")

    async def _handle_finished_recordings(self, finished_room_ids: list):
        """处理已完成的录制"""
        for room_id in finished_room_ids:
            async with async_session() as session:
                result = await session.execute(
                    select(Recording).where(
                        Recording.room_id == room_id,
                        Recording.status == "recording"
                    ).order_by(Recording.id.desc()).limit(1)
                )
                recording = result.scalar_one_or_none()

                if recording:
                    file_size = await recorder.get_file_size(recording.file_path or "")
                    now = datetime.utcnow()
                    duration = (now - recording.started_at).total_seconds() if recording.started_at else 0

                    await session.execute(
                        update(Recording).where(Recording.id == recording.id).values(
                            status="completed",
                            file_size=file_size,
                            duration=duration,
                            ended_at=now,
                        )
                    )

                await session.execute(
                    update(Room).where(Room.id == room_id).values(is_recording=False)
                )
                await session.commit()

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
