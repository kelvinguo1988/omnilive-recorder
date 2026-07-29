"""房间管理API"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import Response
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional, List
from app.database import get_db
from app.models import Room, Recording
from app.services.platform import PlatformFactory
from app.services.monitor import monitor

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class RoomCreate(BaseModel):
    url: str
    platform: Optional[str] = None
    quality: str = "origin"
    enabled: bool = True
    remark: Optional[str] = None
    streamer_name: Optional[str] = None


class RoomUpdate(BaseModel):
    quality: Optional[str] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = None


class RoomImportItem(BaseModel):
    """单个房间导入项（与导出格式对齐）"""
    url: str
    platform: Optional[str] = None
    quality: Optional[str] = "origin"
    enabled: Optional[bool] = True
    remark: Optional[str] = None
    streamer_name: Optional[str] = None


class RoomsImport(BaseModel):
    rooms: List[RoomImportItem]


@router.get("")
async def list_rooms(db: AsyncSession = Depends(get_db)):
    """获取房间列表"""
    result = await db.execute(select(Room).order_by(Room.id.desc()))
    rooms = result.scalars().all()

    return [
        {
            "id": r.id,
            "url": r.url,
            "platform": r.platform,
            "room_id": r.room_id,
            "title": r.title,
            "streamer_name": r.streamer_name,
            "quality": r.quality,
            "enabled": r.enabled,
            "is_live": r.is_live,
            "is_recording": r.is_recording,
            "last_check_time": r.last_check_time.isoformat() if r.last_check_time else None,
            "last_live_time": r.last_live_time.isoformat() if r.last_live_time else None,
            "remark": r.remark,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rooms
    ]


@router.post("")
async def create_room(room: RoomCreate, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """添加房间"""
    # 自动检测平台
    platform = room.platform
    if not platform:
        platform = PlatformFactory.detect_platform(room.url)
        if not platform:
            raise HTTPException(status_code=400, detail="无法识别平台，请手动指定平台(douyin/bilibili/kuaishou)")

    # 检查是否已存在
    existing = await db.execute(select(Room).where(Room.url == room.url))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该房间已存在")

    new_room = Room(
        url=room.url,
        platform=platform,
        quality=room.quality,
        enabled=room.enabled,
        remark=room.remark,
        streamer_name=room.streamer_name,
    )
    db.add(new_room)
    await db.commit()
    await db.refresh(new_room)

    # 后台立即检查一次房间状态
    background_tasks.add_task(monitor.check_room_now, new_room.id)

    return {
        "id": new_room.id,
        "url": new_room.url,
        "platform": new_room.platform,
        "message": "添加成功，正在检测直播状态...",
    }


@router.get("/export")
async def export_rooms(db: AsyncSession = Depends(get_db)):
    """导出所有房间为 JSON（备份 / 迁移用）"""
    result = await db.execute(select(Room).order_by(Room.id))
    rooms = result.scalars().all()
    data = {
        "version": 1,
        "type": "omnilive-rooms",
        "exported_at": datetime.utcnow().isoformat(),
        "count": len(rooms),
        "rooms": [
            {
                "url": r.url,
                "platform": r.platform,
                "quality": r.quality,
                "enabled": r.enabled,
                "remark": r.remark,
                "streamer_name": r.streamer_name or "",
            }
            for r in rooms
        ],
    }
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=rooms_export.json"},
    )


@router.post("/import")
async def import_rooms(payload: RoomsImport, db: AsyncSession = Depends(get_db)):
    """从 JSON 批量导入房间（跳过已存在的 URL）"""
    imported = 0
    skipped = 0
    failed = 0
    errors = []

    for item in payload.rooms:
        url = (item.url or "").strip()
        if not url:
            failed += 1
            errors.append("空 URL，已跳过")
            continue

        platform = item.platform
        if not platform:
            platform = PlatformFactory.detect_platform(url)
            if not platform:
                failed += 1
                errors.append(f"{url}: 无法识别平台")
                continue

        existing = await db.execute(select(Room).where(Room.url == url))
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        try:
            room = Room(
                url=url,
                platform=platform,
                quality=item.quality or "origin",
                enabled=bool(item.enabled) if item.enabled is not None else True,
                remark=item.remark,
                streamer_name=item.streamer_name or None,
            )
            db.add(room)
            await db.commit()
            imported += 1
        except Exception as e:
            await db.rollback()
            failed += 1
            errors.append(f"{url}: {e}")

    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
        "message": f"导入完成：新增 {imported}，跳过 {skipped}，失败 {failed}",
    }


@router.put("/{room_id}")
async def update_room(room_id: int, room: RoomUpdate, db: AsyncSession = Depends(get_db)):
    """更新房间"""
    update_data = room.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    result = await db.execute(select(Room).where(Room.id == room_id))
    existing = result.scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="房间不存在")

    await db.execute(update(Room).where(Room.id == room_id).values(**update_data))
    await db.commit()

    return {"message": "更新成功"}


@router.delete("/{room_id}")
async def delete_room(room_id: int, db: AsyncSession = Depends(get_db)):
    """删除房间"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    # 如果正在录制，先停止
    if room.is_recording:
        await monitor._stop_recording(room)

    await db.execute(delete(Room).where(Room.id == room_id))
    await db.commit()

    return {"message": "删除成功"}


@router.post("/{room_id}/check")
async def check_room(room_id: int, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """手动检查房间状态"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    background_tasks.add_task(monitor.check_room_now, room_id)

    return {"message": "正在检测..."}


@router.post("/{room_id}/start-recording")
async def manual_start_recording(room_id: int, db: AsyncSession = Depends(get_db)):
    """手动开始录制"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    if room.is_recording:
        raise HTTPException(status_code=400, detail="已在录制中")

    if not room.is_live:
        raise HTTPException(status_code=400, detail="未在直播中")

    # 获取流地址
    platform = monitor._get_platform(room.platform)
    if not platform:
        raise HTTPException(status_code=500, detail="平台适配器不可用")

    info = await platform.get_room_info(room.url)
    if not info.stream_url:
        raise HTTPException(status_code=400, detail="无法获取直播流地址")

    await monitor._start_recording(room, info)

    return {"message": "录制已启动"}


@router.post("/{room_id}/stop-recording")
async def manual_stop_recording(room_id: int, db: AsyncSession = Depends(get_db)):
    """手动停止录制"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    if not room.is_recording:
        raise HTTPException(status_code=400, detail="未在录制中")

    await monitor._stop_recording(room)

    return {"message": "录制已停止"}


@router.get("/{room_id}/recordings")
async def get_room_recordings(room_id: int, db: AsyncSession = Depends(get_db)):
    """获取房间的录制记录"""
    result = await db.execute(
        select(Recording).where(Recording.room_id == room_id).order_by(Recording.id.desc())
    )
    recordings = result.scalars().all()

    def _part_count(rec):
        if rec.part_paths:
            try:
                return len(json.loads(rec.part_paths))
            except (ValueError, TypeError):
                return 1
        return 1

    return [
        {
            "id": r.id,
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
            "part_count": _part_count(r),
        }
        for r in recordings
    ]
