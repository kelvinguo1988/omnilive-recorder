"""系统状态API"""
import json
import psutil
import platform
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Room, Recording, SystemLog
from app.services.file_manager import file_manager
from app.services.recorder import recorder
from app.config import settings, save_config

router = APIRouter(prefix="/api/system", tags=["system"])


class SettingsUpdate(BaseModel):
    """系统设置更新请求（所有字段均可选，仅更新传入项）"""
    record_format: Optional[str] = None
    video_quality: Optional[str] = None
    segment_time: Optional[int] = None
    max_retries: Optional[int] = None
    retry_delay: Optional[int] = None
    monitor_interval: Optional[int] = None
    check_timeout: Optional[int] = None
    output_dir: Optional[str] = None
    max_disk_usage: Optional[int] = None
    filename_template: Optional[str] = None
    enable_notification: Optional[bool] = None
    webhook_url: Optional[str] = None
    enable_proxy: Optional[bool] = None
    proxy_addr: Optional[str] = None
    douyin_cookie: Optional[str] = None
    bilibili_cookie: Optional[str] = None
    kuaishou_cookie: Optional[str] = None


_VALID_FORMATS = {"ts", "flv", "mp4"}
_INT_FIELDS = ("segment_time", "monitor_interval", "check_timeout",
               "max_retries", "retry_delay", "max_disk_usage")
_PROXY_RELATED = ("proxy_addr", "enable_proxy", "douyin_cookie", "bilibili_cookie", "kuaishou_cookie")


@router.get("/info")
async def system_info(db: AsyncSession = Depends(get_db)):
    """获取系统信息"""
    # 房间统计
    room_count = await db.scalar(select(func.count(Room.id)))
    live_count = await db.scalar(select(func.count(Room.id)).where(Room.is_live == True))
    recording_count = await db.scalar(select(func.count(Room.id)).where(Room.is_recording == True))
    enabled_count = await db.scalar(select(func.count(Room.id)).where(Room.enabled == True))

    # 录制统计
    total_recordings = await db.scalar(select(func.count(Recording.id)))
    completed_recordings = await db.scalar(
        select(func.count(Recording.id)).where(Recording.status == "completed")
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
            "video_quality": settings.video_quality,
            "segment_time": settings.segment_time,
            "max_retries": settings.max_retries,
            "retry_delay": settings.retry_delay,
            "monitor_interval": settings.monitor_interval,
            "check_timeout": settings.check_timeout,
            "output_dir": settings.output_dir,
            "max_disk_usage": settings.max_disk_usage,
            "filename_template": settings.filename_template,
            "enable_notification": settings.enable_notification,
            "webhook_url": settings.webhook_url,
            "enable_proxy": settings.enable_proxy,
            "proxy_addr": settings.proxy_addr,
            "douyin_cookie": settings.douyin_cookie,
            "bilibili_cookie": settings.bilibili_cookie,
            "kuaishou_cookie": settings.kuaishou_cookie,
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


@router.put("/settings")
async def update_settings(data: SettingsUpdate):
    """更新系统设置（运行时生效 + 持久化到配置文件）"""
    updates = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
        if v is not None
    }
    return await _apply_settings(updates)


@router.post("/settings/import")
async def import_settings(data: dict):
    """从 JSON 批量导入系统设置（用于备份 / 迁移）

    仅接受已知设置字段；布尔值为 false 时也保留（不会被过滤）。
    """
    known = set(SettingsUpdate.model_fields.keys())
    updates = {k: v for k, v in data.items() if k in known and v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何有效的设置项")
    return await _apply_settings(updates)


async def _apply_settings(updates: dict):
    """校验 + 应用 + 持久化设置（PUT 与 import 共用）"""
    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何要更新的设置项")

    # 校验录制格式
    if "record_format" in updates and updates["record_format"] not in _VALID_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"无效的录制格式: {updates['record_format']}，可选值: ts/flv/mp4",
        )

    # 校验文件名模板
    if "filename_template" in updates:
        tmpl = updates["filename_template"] or ""
        if not tmpl or "/" in tmpl or "\\" in tmpl:
            raise HTTPException(
                status_code=400,
                detail="文件名模板不能为空且不能包含路径分隔符（/ 或 \\）",
            )
        # 占位符替换后必须非空、且不能只剩点号，避免生成空文件名
        _test = tmpl
        for _k in ("streamer", "room_id", "platform", "title", "date", "time", "datetime"):
            _test = _test.replace("{" + _k + "}", "x")
        if not _test.strip().strip("."):
            raise HTTPException(
                status_code=400,
                detail="文件名模板替换后为空，请至少包含 {streamer}/{room_id}/{time} 等占位符之一",
            )

    # 校验整数型字段
    for field in _INT_FIELDS:
        if field in updates:
            try:
                val = int(updates[field])
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"{field} 必须为整数")
            if val <= 0:
                raise HTTPException(status_code=400, detail=f"{field} 必须为正数")
            updates[field] = val

    # 应用：更新全局 settings 实例（后续录制/监控自动读取新值）
    proxy_related_changed = False
    for key, value in updates.items():
        old = getattr(settings, key, None)
        if old != value:
            setattr(settings, key, value)
            if key in _PROXY_RELATED:
                proxy_related_changed = True

    # 输出目录变化：确保目录存在
    if "output_dir" in updates:
        os.makedirs(settings.output_dir, exist_ok=True)

    # 代理/cookie 变化：清空已缓存的平台适配器实例，下次检查时按新配置重建
    if proxy_related_changed:
        from app.services.monitor import monitor
        monitor._platform_instances.clear()

    # 持久化到配置文件，重启后依然生效
    try:
        save_config(settings)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e}")

    return {
        "success": True,
        "message": "设置已更新",
        "settings": {
            "record_format": settings.record_format,
            "video_quality": settings.video_quality,
            "segment_time": settings.segment_time,
            "max_retries": settings.max_retries,
            "retry_delay": settings.retry_delay,
            "monitor_interval": settings.monitor_interval,
            "check_timeout": settings.check_timeout,
            "output_dir": settings.output_dir,
            "max_disk_usage": settings.max_disk_usage,
            "filename_template": settings.filename_template,
            "enable_notification": settings.enable_notification,
            "webhook_url": settings.webhook_url,
            "enable_proxy": settings.enable_proxy,
            "proxy_addr": settings.proxy_addr,
            "douyin_cookie": settings.douyin_cookie,
            "bilibili_cookie": settings.bilibili_cookie,
            "kuaishou_cookie": settings.kuaishou_cookie,
        },
    }


@router.get("/settings/export")
async def export_settings():
    """导出当前系统设置为 JSON（备份 / 迁移用）"""
    s = settings
    data = {
        "version": 1,
        "type": "omnilive-settings",
        "exported_at": datetime.utcnow().isoformat(),
        "settings": {
            "record_format": s.record_format,
            "video_quality": s.video_quality,
            "segment_time": s.segment_time,
            "max_retries": s.max_retries,
            "retry_delay": s.retry_delay,
            "monitor_interval": s.monitor_interval,
            "check_timeout": s.check_timeout,
            "output_dir": s.output_dir,
            "max_disk_usage": s.max_disk_usage,
            "filename_template": s.filename_template,
            "enable_notification": s.enable_notification,
            "webhook_url": s.webhook_url,
            "enable_proxy": s.enable_proxy,
            "proxy_addr": s.proxy_addr,
            "douyin_cookie": s.douyin_cookie,
            "bilibili_cookie": s.bilibili_cookie,
            "kuaishou_cookie": s.kuaishou_cookie,
        },
    }
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=settings_export.json"},
    )
