"""作品订阅API - 作品记录管理（主播的添加/编辑/订阅开关在主播管理 /api/rooms）"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Room, Work
from app.services.platform.base import PLATFORM_CN
from app.services.works_monitor import works_monitor

router = APIRouter(prefix="/api/works", tags=["works"])


@router.get("")
async def list_works(room_id: int = None, status: str = None, limit: int = 200,
                     db: AsyncSession = Depends(get_db)):
    """作品列表（可按主播/状态过滤）"""
    query = (
        select(Work, Room)
        .join(Room, Work.creator_id == Room.id)
        .order_by(Work.publish_time.desc().nullslast(), Work.id.desc())
        .limit(min(limit, 500))
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
            "publish_time": w.publish_time.isoformat() if w.publish_time else None,
            "duration": round(w.duration, 1) if w.duration else 0,
            "file_path": w.file_path,
            "file_size": w.file_size,
            "file_size_mb": round(w.file_size / 1024 / 1024, 2) if w.file_size else 0,
            "status": w.status,
            "error_message": w.error_message,
            "downloaded_at": w.downloaded_at.isoformat() if w.downloaded_at else None,
            "created_at": w.created_at.isoformat() if w.created_at else None,
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
    return {"message": "已加入下载队列"}


@router.delete("/{work_id}")
async def delete_work(work_id: int, db: AsyncSession = Depends(get_db)):
    """删除作品记录并删除已下载文件"""
    result = await db.execute(select(Work).where(Work.id == work_id))
    work = result.scalar_one_or_none()
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")

    if work.file_path:
        from app.services.file_manager import file_manager
        try:
            file_manager.delete_file(work.file_path)
        except Exception:
            pass

    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(Work).where(Work.id == work_id))
    await db.commit()
    return {"message": "删除成功"}
