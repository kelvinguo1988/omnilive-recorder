"""NAS 同步服务 - 定时把已完成的直播录制/作品按主播归拢到挂载目录（QNAP 等）

零配置按主播分文件夹：只需在设置里填一个同步根目录，目标布局自动为：

    {同步根}/{主播名}/直播/{平台}_{文件名}     ← 直播录制
    {同步根}/{主播名}/作品/{文件名}            ← 作品（视频/图集zip）

- 数据源是数据库记录：只同步 status=completed 的录制与作品，
  录制中的文件未最终化，绝不同步
- 增量幂等：目标已存在且大小一致 → 跳过；.part 临时文件原子改名
- 单向累积（备份语义）：源删除不影响已同步副本
- 主播名取 rooms.streamer_name，为空回退 platform_user_id；非法字符自动清洗
"""
import asyncio
import os
import shutil
import time
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import Room, Recording, Work
from app.config import settings
from app.services.recorder import recorder
from app.services.platform.base import PLATFORM_CN

logger = logging.getLogger(__name__)


class SyncService:
    """定期把已完成内容按主播同步到 NAS 挂载目录"""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._sync_lock = asyncio.Lock()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("NAS 同步服务已启动")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("NAS 同步服务已停止")

    async def _loop(self):
        await asyncio.sleep(10)
        while self._running:
            if settings.sync_enabled and settings.sync_root:
                try:
                    await self.sync_now()
                except Exception as e:
                    logger.error(f"NAS 同步轮次异常: {e}")
            # 间隔下限 60s，防止误配过短打爆磁盘
            interval = max(settings.sync_interval, 60)
            waited = 0.0
            while self._running and waited < interval:
                step = min(5.0, interval - waited)
                await asyncio.sleep(step)
                waited += step

    async def sync_now(self) -> dict:
        """执行一轮同步（循环与手动触发共用），返回统计"""
        async with self._sync_lock:
            return await self._sync_all_inner()

    async def _sync_all_inner(self) -> dict:
        if not settings.sync_root:
            return {"copied": 0, "skipped": 0, "failed": 0, "error": "未配置同步根目录"}
        root = os.path.abspath(settings.sync_root)
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as e:
            return {"copied": 0, "skipped": 0, "failed": 0, "error": f"同步目录不可写: {e}"}

        async with async_session() as session:
            rec_rows = (await session.execute(
                select(Recording, Room)
                .join(Room, Recording.room_id == Room.id)
                .where(Recording.status == "completed")
            )).all()
            work_rows = (await session.execute(
                select(Work, Room)
                .join(Room, Work.creator_id == Room.id)
                .where(Work.status == "completed")
            )).all()

        copied = skipped = failed = 0

        for rec, room in rec_rows:
            if not rec.file_path or not rec.file_name:
                continue
            src = os.path.join(settings.output_dir, rec.file_path)
            if not os.path.isfile(src):
                continue
            # 平台前缀防跨平台同名主播文件混淆：B站_live1.ts
            dst = os.path.join(
                self._streamer_dir(root, room), "直播",
                f"{PLATFORM_CN.get(room.platform, room.platform)}_{rec.file_name}",
            )
            c, s = await asyncio.to_thread(self._copy_if_needed, src, dst)
            copied, skipped, failed = copied + max(c, 0), skipped + s, failed + (1 if c < 0 else 0)

        for work, room in work_rows:
            if not work.file_path:
                continue
            src = os.path.join(settings.output_dir, work.file_path)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(self._streamer_dir(root, room), "作品", os.path.basename(work.file_path))
            c, s = await asyncio.to_thread(self._copy_if_needed, src, dst)
            copied, skipped, failed = copied + max(c, 0), skipped + s, failed + (1 if c < 0 else 0)

        if copied or failed:
            logger.info(f"NAS 同步完成: 新复制 {copied}，跳过 {skipped}，失败 {failed}")
        return {"copied": copied, "skipped": skipped, "failed": failed}

    @staticmethod
    def _streamer_dir(root: str, room: Room) -> str:
        """{同步根}/{主播名}：主播名清洗非法字符，空则回退平台用户ID/主播ID"""
        name = room.streamer_name or room.platform_user_id or f"主播{room.id}"
        return os.path.join(root, recorder._sanitize_filename(name))

    @staticmethod
    def _copy_if_needed(src: str, dst: str) -> tuple:
        """增量复制：目标存在且大小一致跳过。返回 (copied, skipped)；失败 copied=-1"""
        try:
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                return 0, 1
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            part = dst + ".part"
            shutil.copy2(src, part)
            os.replace(part, dst)
            return 1, 0
        except Exception as e:
            logger.error(f"同步文件失败 {src} -> {dst}: {e}")
            return -1, 0


# 全局同步服务实例
sync_service = SyncService()
