"""录制记录API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Recording, Room

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


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
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "error_message": r.error_message,
        }
        for r, room in rows
    ]


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """获取统计信息"""
    result = await db.execute(select(Recording))
    recordings = result.scalars().all()

    total = len(recordings)
    completed = sum(1 for r in recordings if r.status == "completed")
    recording = sum(1 for r in recordings if r.status == "recording")
    failed = sum(1 for r in recordings if r.status == "failed")
    total_size = sum(r.file_size or 0 for r in recordings)
    total_duration = sum(r.duration or 0 for r in recordings)

    return {
        "total": total,
        "completed": completed,
        "recording": recording,
        "failed": failed,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "total_size_gb": round(total_size / 1024 / 1024 / 1024, 2),
        "total_duration_hours": round(total_duration / 3600, 1),
    }
