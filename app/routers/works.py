"""作品订阅API - 作品记录管理（主播的添加/编辑/订阅开关在主播管理 /api/rooms）"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Room, Work
from app.config import settings
from app.utils import iso
from app.services.platform.base import PLATFORM_CN
from app.services.works_monitor import works_monitor

router = APIRouter(prefix="/api/works", tags=["works"])

logger = logging.getLogger(__name__)


@router.get("")
async def list_works(room_id: Optional[int] = None, status: Optional[str] = None,
                     limit: int = 200, db: AsyncSession = Depends(get_db)):
    """作品列表（可按主播/状态过滤）"""
    query = (
        select(Work, Room)
        .join(Room, Work.creator_id == Room.id)
        .order_by(Work.publish_time.desc().nullslast(), Work.id.desc())
        # 下限 1：SQLite 把 LIMIT -1 解释为"无上限"，负数会整表返回
        .limit(max(1, min(limit, 500)))
    )
    if room_id is not None:
        query = query.where(Work.creator_id == room_id)
    if status:
        query = query.where(Work.status == status)

    rows = (await db.execute(query)).all()
    return [
        {
            "id": w.id,
            "room_id": w.creator_id,
            "nickname": c.streamer_name,
            "platform": c.platform,
            "platform_name": PLATFORM_CN.get(c.platform, c.platform),
            "platform_work_id": w.platform_work_id,
            "work_type": w.work_type,
            "title": w.title,
            "publish_time": iso(w.publish_time),
            "duration": round(w.duration, 1) if w.duration else 0,
            "file_path": w.file_path,
            "file_size": w.file_size,
            "file_size_mb": round(w.file_size / 1024 / 1024, 2) if w.file_size else 0,
            "status": w.status,
            "error_message": w.error_message,
            "downloaded_at": iso(w.downloaded_at),
            "created_at": iso(w.created_at),
        }
        for w, c in rows
    ]


@router.post("/check/{room_id}")
async def check_room_works_now(room_id: int, background_tasks: BackgroundTasks,
                               db: AsyncSession = Depends(get_db)):
    """立即检查主播的新作品（未回填则先回填）"""
    result = await db.execute(select(Room.id).where(Room.id == room_id))
    if not result.first():
        raise HTTPException(status_code=404, detail="主播不存在")
    background_tasks.add_task(works_monitor.check_room_works_by_id, room_id)
    return {"message": "正在检查作品..."}


@router.post("/{work_id}/retry")
async def retry_work(work_id: int, background_tasks: BackgroundTasks,
                     db: AsyncSession = Depends(get_db)):
    """重试失败/已完成的下载（重新置为待下载）"""
    result = await db.execute(select(Work).where(Work.id == work_id))
    work = result.scalar_one_or_none()
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    if work.status == "downloading":
        raise HTTPException(status_code=400, detail="正在下载中")

    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Work).where(Work.id == work_id).values(
            status="pending", error_message=None,
        )
    )
    await db.commit()
    background_tasks.add_task(works_monitor.process_download_queue)
    if not settings.works_auto_download:
        return {"message": "已置为待下载，但「作品自动下载」当前已关闭，不会实际下载（可在设置页开启）"}
    return {"message": "已加入下载队列"}


@router.delete("/{work_id}")
async def delete_work(work_id: int, db: AsyncSession = Depends(get_db)):
    """删除作品记录并删除已下载文件"""
    result = await db.execute(select(Work).where(Work.id == work_id))
    work = result.scalar_one_or_none()
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    # 与 retry 一致的在途保护：下载中删除会让队列稍后把新文件写到一个已无
    # DB 记录的路径上，下次启动 import_existing_files 又把它认领回来——
    # "已删除"的作品静默复活
    if work.status == "downloading":
        raise HTTPException(status_code=400, detail="正在下载中，请等待下载结束后再删除")

    if work.file_path:
        from app.services.file_manager import file_manager
        try:
            file_manager.delete_file(work.file_path)
        except Exception as e:  # noqa: BLE001
            # 文件删除失败不阻断记录删除，但必须留痕——否则磁盘孤儿无从排查
            logger.warning(f"删除作品文件失败 {work.file_path}: {e}")

    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(Work).where(Work.id == work_id))
    await db.commit()
    return {"message": "删除成功"}
