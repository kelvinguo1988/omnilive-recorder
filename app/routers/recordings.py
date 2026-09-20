"""录制记录API"""
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Recording, Room
from app.utils import iso

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


def _part_count(rec: Recording) -> int:
    if rec.part_paths:
        try:
            return len(json.loads(rec.part_paths))
        except (ValueError, TypeError):
            return 1
    return 1


@router.get("")
async def list_recordings(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    """获取录制记录列表"""
    result = await db.execute(
        select(Recording, Room)
        .join(Room, Recording.room_id == Room.id)
        .order_by(Recording.id.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = result.all()

    return [
        {
            "id": r.id,
            "room_id": r.room_id,
            "platform": room.platform,
            "streamer_name": room.streamer_name,
            "room_title": room.title,
            "file_path": r.file_path,
            "file_name": r.file_name,
            "file_size": r.file_size,
            "file_size_mb": round(r.file_size / 1024 / 1024, 2) if r.file_size else 0,
            "duration": round(r.duration, 1) if r.duration else 0,
            "format": r.format,
            "status": r.status,
            "started_at": iso(r.started_at),
            "ended_at": iso(r.ended_at),
            "error_message": r.error_message,
            "part_count": _part_count(r),
        }
        for r, room in rows
    ]


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """获取统计信息（单条聚合查询，避免全表载入内存）"""
    from sqlalchemy import case

    row = (await db.execute(
        select(
            func.count(Recording.id),
            func.sum(case((Recording.status == "completed", 1), else_=0)),
            func.sum(case((Recording.status == "recording", 1), else_=0)),
            func.sum(case((Recording.status == "failed", 1), else_=0)),
            func.coalesce(func.sum(func.coalesce(Recording.file_size, 0)), 0),
            func.coalesce(func.sum(func.coalesce(Recording.duration, 0)), 0),
        )
    )).one()
    total, completed, recording, failed, total_size, total_duration = row

    return {
        "total": total or 0,
        "completed": completed or 0,
        "recording": recording or 0,
        "failed": failed or 0,
        "total_size_mb": round((total_size or 0) / 1024 / 1024, 2),
        "total_size_gb": round((total_size or 0) / 1024 / 1024 / 1024, 2),
        "total_duration_hours": round((total_duration or 0) / 3600, 1),
    }
