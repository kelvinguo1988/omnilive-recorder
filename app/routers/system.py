"""系统状态API"""
import psutil
import platform
import os
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Room, Recording, SystemLog
from app.services.file_manager import file_manager
from app.services.recorder import recorder
from app.config import settings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
async def system_info(db: AsyncSession = Depends(get_db)):
    """获取系统信息"""
    # 房间统计
    room_count = await db.scalar(select(func.count(Room.id)))
    live_count = await db.scalar(select(func.count(Room.id).where(Room.is_live == True)))
    recording_count = await db.scalar(select(func.count(Room.id).where(Room.is_recording == True)))
    enabled_count = await db.scalar(select(func.count(Room.id).where(Room.enabled == True)))

    # 录制统计
    total_recordings = await db.scalar(select(func.count(Recording.id)))
    completed_recordings = await db.scalar(
        select(func.count(Recording.id).where(Recording.status == "completed"))
    )

    # 磁盘使用
    disk = file_manager.get_disk_usage()

    # 系统信息
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()

    return {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "memory_total_gb": round(memory.total / 1024 / 1024 / 1024, 2),
        "memory_used_gb": round(memory.used / 1024 / 1024 / 1024, 2),
        "memory_percent": memory.percent,
        "disk": disk,
        "rooms": {
            "total": room_count,
            "enabled": enabled_count,
            "live": live_count,
            "recording": recording_count,
        },
        "recordings": {
            "total": total_recordings,
            "completed": completed_recordings,
        },
        "active_recordings": len(recorder.active_processes),
        "settings": {
            "record_format": settings.record_format,
            "monitor_interval": settings.monitor_interval,
            "segment_time": settings.segment_time,
            "output_dir": settings.output_dir,
        },
    }


@router.get("/logs")
async def get_logs(limit: int = 100, level: str = None, db: AsyncSession = Depends(get_db)):
    """获取系统日志"""
    query = select(SystemLog).order_by(SystemLog.id.desc()).limit(limit)
    if level:
        query = query.where(SystemLog.level == level)

    result = await db.execute(query)
    logs = result.scalars().all()

    return [
        {
            "id": log.id,
            "level": log.level,
            "module": log.module,
            "message": log.message,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


@router.get("/platforms")
async def get_platforms():
    """获取支持的平台列表"""
    from app.services.platform import PlatformFactory

    platforms = PlatformFactory.get_all_platforms()
    platform_info = {
        "douyin": {"name": "抖音", "color": "#FE2C55", "url_example": "https://live.douyin.com/123456789"},
        "bilibili": {"name": "B站", "color": "#00A1D6", "url_example": "https://live.bilibili.com/12345"},
        "kuaishou": {"name": "快手", "color": "#FF4906", "url_example": "https://live.kuaishou.com/u/xxx"},
    }

    return [
        {
            "key": p,
            "name": platform_info.get(p, {}).get("name", p),
            "color": platform_info.get(p, {}).get("color", "#888"),
            "url_example": platform_info.get(p, {}).get("url_example", ""),
        }
        for p in platforms
    ]
