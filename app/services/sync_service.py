"""NAS 同步服务 - 把已完成的直播录制/作品按主播归拢同步到挂载目录（QNAP 等）

设计：
- 只同步 DB 中 status=completed 的录制记录与作品记录（录制中的文件未最终化，不同步）
- 目标布局：{sync_root}/{主播同步路径}/直播/{文件名} 与 {sync_root}/{主播同步路径}/作品/{文件名}
  主播同步路径 = rooms.sync_path，未配置时用主播名（platform_user_id 兜底）
- 增量幂等：目标存在且大小一致跳过；复制走 .part 临时文件原子改名
- 单向累积：源删除不影响已同步副本（备份语义）
"""
import asyncio
import os
import shutil
import time
import logging
from typing import Optional

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
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 重入保护：手动触发与循环不并发
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
                    await self.sync_all()
                except Exception as e:
                    logger.error(f"NAS 同步轮次异常: {e}")
            interval = max(settings.sync_interval, 60) if settings.sync_interval > 0 else 300
            waited = 0.0
            while self._running and waited < interval:
                step = min(5.0, interval - waited)
                await asyncio.sleep(step)
                waited += step

    # ---------- 同步逻辑 ----------

    def _room_sync_dir(self, room: Room, subdir: str) -> str:
        """主播的目标目录：{sync_root}/{主播同步路径}/{直播|作品}

        sync_path 支持多级子路径（如 综艺/主播A），逐段清洗并拒绝 . / .. 逃逸；
        未配置时用主播名（platform_user_id 兜底）。
        """
        default_base = room.streamer_name or room.platform_user_id or f"主播{room.id}"
        base = (room.sync_path or "").strip()
        if base:
            parts = [recorder._sanitize_filename(seg) for seg in base.split("/")
                     if seg.strip() and seg.strip() not in (".", "..")]
            base = "/".join(p for p in parts if p) or default_base
        else:
            base = default_base
        return os.path.join(settings.sync_root, base, subdir)

    async def sync_all(self) -> dict:
        """同步全部主播，返回统计（手动触发与循环共用）"""
        async with self._sync_lock:
            return await self._sync_all_inner()

    async def _sync_all_inner(self) -> dict:
        if not settings.sync_root:
            return {"copied": 0, "skipped": 0, "failed": 0, "error": "未配置同步根目录"}

        root = os.path.abspath(settings.sync_root)
        os.makedirs(root, exist_ok=True)

        async with async_session() as session:
            # 已完成的录制记录（join 主播拿同步路径）
            rec_rows = (await session.execute(
                select(Recording, Room)
                .join(Room, Recording.room_id == Room.id)
                .where(Recording.status == "completed")
            )).all()
            # 已完成的作品
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
            # 直播录制按平台区分子目录，避免不同平台同名主播混淆：
            # {sync_root}/{主播路径}/直播/{平台}_{文件名}
            dst_dir = self._room_sync_dir(room, "直播")
            dst = os.path.join(dst_dir, f"{PLATFORM_CN.get(room.platform, room.platform)}_{rec.file_name}")
            c, s = await asyncio.to_thread(self._copy_if_needed, src, dst)
            copied += c
            skipped += s
            if c < 0:
                failed += 1

        for work, room in work_rows:
            if not work.file_path:
                continue
            src = os.path.join(settings.output_dir, work.file_path)
            if not os.path.isfile(src):
                continue
            dst_dir = self._room_sync_dir(room, "作品")
            dst = os.path.join(dst_dir, os.path.basename(work.file_path))
            c, s = await asyncio.to_thread(self._copy_if_needed, src, dst)
            copied += c
            skipped += s
            if c < 0:
                failed += 1

        if copied or failed:
            logger.info(f"NAS 同步完成: 新复制 {copied}，已存在跳过 {skipped}，失败 {failed}")
        return {"copied": copied, "skipped": skipped, "failed": failed}

    @staticmethod
    def _copy_if_needed(src: str, dst: str) -> tuple:
        """增量复制：目标存在且大小一致跳过。返回 (copied, skipped)；失败 copied=-1。

        防御：源必须在 output_dir 内、目标必须在 sync_root 内（防止 sync_path 配置
        注入 ../ 逃逸到挂载目录之外）。
        """
        try:
            root = os.path.abspath(settings.sync_root)
            dst_abs = os.path.abspath(dst)
            if not dst_abs.startswith(root + os.sep):
                logger.warning(f"同步目标越界，跳过: {dst}")
                return -1, 0

            if os.path.isfile(dst_abs) and os.path.getsize(dst_abs) == os.path.getsize(src):
                return 0, 1

            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
            part = dst_abs + ".part"
            shutil.copy2(src, part)
            os.replace(part, dst_abs)
            return 1, 0
        except Exception as e:
            logger.error(f"同步文件失败 {src} -> {dst}: {e}")
            return -1, 0


# 全局同步服务实例
sync_service = SyncService()
