"""主播管理API（统一实体：直播间地址=直播录制，主页地址=作品订阅）"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import Response
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional, List
from app.database import get_db
from app.models import Room, Recording, Work
from app.services.platform import PlatformFactory
from app.services.platform.base import PLATFORM_CN
from app.services.monitor import monitor

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class RoomCreate(BaseModel):
    url: Optional[str] = ""            # 直播间地址（与 home_url 至少一个）
    home_url: Optional[str] = ""       # 主页地址（作品订阅依据）
    platform: Optional[str] = None
    quality: str = "origin"
    enabled: bool = True
    works_enabled: bool = False
    remark: Optional[str] = None
    streamer_name: Optional[str] = None


class RoomUpdate(BaseModel):
    url: Optional[str] = None
    home_url: Optional[str] = None
    platform: Optional[str] = None
    quality: Optional[str] = None
    enabled: Optional[bool] = None
    works_enabled: Optional[bool] = None
    remark: Optional[str] = None
    streamer_name: Optional[str] = None


class RoomImportItem(BaseModel):
    """单个主播导入项（与导出格式对齐）"""
    url: Optional[str] = ""
    home_url: Optional[str] = ""
    platform: Optional[str] = None
    quality: Optional[str] = "origin"
    enabled: Optional[bool] = True
    works_enabled: Optional[bool] = False
    remark: Optional[str] = None
    streamer_name: Optional[str] = None


class RoomsImport(BaseModel):
    rooms: List[RoomImportItem]


def _extract_platform_user_id(platform: str, home_url: str) -> str:
    """由主页地址解析平台用户ID（纯URL解析，免网络）"""
    cls = PlatformFactory.get_platform_class(platform)
    return cls.extract_user_id(home_url) if cls and home_url else ""


@router.get("")
async def list_rooms(db: AsyncSession = Depends(get_db)):
    """主播列表（含直播状态与作品订阅统计）"""
    result = await db.execute(select(Room).order_by(Room.id.desc()))
    rooms = result.scalars().all()

    # 作品统计聚合（一次性）
    works_stats = {}
    rows = (await db.execute(
        select(
            Work.creator_id,
            func.count(Work.id),
            func.sum(func.coalesce(Work.file_size, 0)),
            func.max(Work.publish_time),
        )
        .group_by(Work.creator_id)
    )).all()
    for room_id, total, size, latest in rows:
        works_stats[room_id] = {"total": total, "size": size or 0, "latest": latest}
    status_rows = (await db.execute(
        select(Work.creator_id, Work.status, func.count(Work.id))
        .group_by(Work.creator_id, Work.status)
    )).all()
    for room_id, status, cnt in status_rows:
        works_stats.setdefault(room_id, {"total": 0, "size": 0, "latest": None})
        works_stats[room_id][f"{status}_count"] = cnt

    return [
        {
            "id": r.id,
            "url": r.url,
            "home_url": r.home_url or "",
            "platform": r.platform,
            "platform_name": PLATFORM_CN.get(r.platform, r.platform),
            "room_id": r.room_id,
            "platform_user_id": r.platform_user_id,
            "title": r.title,
            "streamer_name": r.streamer_name,
            "quality": r.quality,
            "enabled": r.enabled,
            "works_enabled": bool(r.works_enabled),
            "backfill_done": bool(r.backfill_done),
            "is_live": r.is_live,
            "is_recording": r.is_recording,
            "last_check_time": r.last_check_time.isoformat() if r.last_check_time else None,
            "last_live_time": r.last_live_time.isoformat() if r.last_live_time else None,
            "last_work_check_time": r.last_work_check_time.isoformat() if r.last_work_check_time else None,
            "remark": r.remark,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "works_total": works_stats.get(r.id, {}).get("total", 0),
            "works_completed": works_stats.get(r.id, {}).get("completed_count", 0),
            "works_pending": works_stats.get(r.id, {}).get("pending_count", 0),
            "works_failed": works_stats.get(r.id, {}).get("failed_count", 0),
            "works_size_mb": round(works_stats.get(r.id, {}).get("size", 0) / 1024 / 1024, 2),
            "works_latest_time": works_stats.get(r.id, {}).get("latest").isoformat()
                if works_stats.get(r.id, {}).get("latest") else None,
        }
        for r in rooms
    ]


@router.post("")
async def create_room(room: RoomCreate, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """添加主播（直播间地址与主页地址至少一个）"""
    url = (room.url or "").strip()
    home_url = (room.home_url or "").strip()
    if not url and not home_url:
        raise HTTPException(status_code=400, detail="直播间地址与主页地址至少填一个")

    # 自动检测平台：优先直播间地址，其次主页地址
    platform = room.platform
    if not platform:
        for candidate in (url, home_url):
            if candidate:
                platform = PlatformFactory.detect_platform(candidate)
                if platform:
                    break
    if not platform:
        raise HTTPException(status_code=400, detail="无法识别平台，请手动指定平台(douyin/bilibili/kuaishou)")

    # 查重：同平台下直播间地址或平台用户ID任一相同即视为同一主播
    platform_user_id = _extract_platform_user_id(platform, home_url)
    dup = await db.execute(
        select(Room).where(
            Room.platform == platform,
            (Room.url == url) if url else (Room.platform_user_id == platform_user_id),
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该主播已存在（直播间地址或主页地址重复）")

    new_room = Room(
        url=url,
        home_url=home_url,
        platform=platform,
        room_id=PlatformFactory.get_platform_class(platform).extract_room_id(url) if url else None,
        platform_user_id=platform_user_id or None,
        quality=room.quality,
        enabled=room.enabled,
        works_enabled=room.works_enabled and bool(platform_user_id),
        remark=room.remark,
        streamer_name=room.streamer_name,
    )
    db.add(new_room)
    await db.commit()
    await db.refresh(new_room)

    # 后台立即检查：有直播间地址则检测直播，有作品订阅则开始回填
    if url:
        background_tasks.add_task(monitor.check_room_now, new_room.id)
    if new_room.works_enabled:
        background_tasks.add_task(works_check_task, new_room.id)

    return {
        "id": new_room.id,
        "url": new_room.url,
        "platform": new_room.platform,
        "platform_user_id": new_room.platform_user_id,
        "message": "添加成功，正在后台检测..." if url else "添加成功（未配置直播间地址，仅作品订阅）",
    }


async def works_check_task(room_id: int):
    from app.services.works_monitor import works_monitor
    await works_monitor.check_room_works_by_id(room_id)


@router.get("/export")
async def export_rooms(db: AsyncSession = Depends(get_db)):
    """导出所有主播为 JSON（备份 / 迁移用）"""
    result = await db.execute(select(Room).order_by(Room.id))
    rooms = result.scalars().all()
    data = {
        "version": 2,
        "type": "omnilive-streamers",
        "exported_at": datetime.now().isoformat(),
        "count": len(rooms),
        "rooms": [
            {
                "url": r.url or "",
                "home_url": r.home_url or "",
                "platform": r.platform,
                "quality": r.quality,
                "enabled": r.enabled,
                "works_enabled": bool(r.works_enabled),
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
        headers={"Content-Disposition": "attachment; filename=streamers_export.json"},
    )


@router.post("/import")
async def import_rooms(payload: RoomsImport, db: AsyncSession = Depends(get_db)):
    """从 JSON 批量导入主播（跳过已存在的）"""
    imported = 0
    skipped = 0
    failed = 0
    errors = []

    for item in payload.rooms:
        url = (item.url or "").strip()
        home_url = (item.home_url or "").strip()
        if not url and not home_url:
            failed += 1
            errors.append("直播间与主页地址均为空，已跳过")
            continue

        platform = item.platform
        if not platform:
            for candidate in (url, home_url):
                if candidate:
                    platform = PlatformFactory.detect_platform(candidate)
                    if platform:
                        break
        if not platform:
            failed += 1
            errors.append(f"{url or home_url}: 无法识别平台")
            continue

        platform_user_id = _extract_platform_user_id(platform, home_url)
        existing = await db.execute(
            select(Room).where(
                Room.platform == platform,
                (Room.url == url) if url else (Room.platform_user_id == platform_user_id),
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        try:
            db.add(Room(
                url=url,
                home_url=home_url,
                platform=platform,
                room_id=PlatformFactory.get_platform_class(platform).extract_room_id(url) if url else None,
                platform_user_id=platform_user_id or None,
                quality=item.quality or "origin",
                enabled=bool(item.enabled) if item.enabled is not None else True,
                works_enabled=bool(item.works_enabled) and bool(platform_user_id),
                remark=item.remark,
                streamer_name=item.streamer_name or None,
            ))
            await db.commit()
            imported += 1
        except Exception as e:
            await db.rollback()
            failed += 1
            errors.append(f"{url or home_url}: {e}")

    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
        "message": f"导入完成：新增 {imported}，跳过 {skipped}，失败 {failed}",
    }


@router.put("/{room_id}")
async def update_room(room_id: int, room: RoomUpdate, background_tasks: BackgroundTasks,
                      db: AsyncSession = Depends(get_db)):
    """更新主播（直播地址/主页地址/画质/开关/备注/主播名）"""
    update_data = room.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    result = await db.execute(select(Room).where(Room.id == room_id))
    existing = result.scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="主播不存在")

    # 文本字段统一去首尾空白，避免存入不可见的空白导致"看起来空实则非空"
    for field in ("url", "home_url", "quality", "remark", "streamer_name"):
        if field in update_data and isinstance(update_data[field], str):
            update_data[field] = update_data[field].strip()

    # 主页地址变化：重新解析平台用户ID；清空主页则同时关闭作品订阅
    if "home_url" in update_data:
        home_url = update_data["home_url"]
        if home_url:
            puid = _extract_platform_user_id(existing.platform, home_url)
            if not puid:
                raise HTTPException(
                    status_code=400,
                    detail=f"无法从主页地址解析用户ID，示例: "
                           f"{'douyin.com/user/MS4w...' if existing.platform == 'douyin' else 'space.bilibili.com/数字' if existing.platform == 'bilibili' else 'kuaishou.com/profile/xxx'}",
                )
            update_data["platform_user_id"] = puid
        else:
            update_data["platform_user_id"] = None
            update_data["works_enabled"] = False

    # 修改直播间地址时同步重算 room_id 与平台（用于文件名 / 适配器选择）
    if "url" in update_data and update_data["url"]:
        new_url = update_data["url"]
        if not update_data.get("platform"):
            detected = PlatformFactory.detect_platform(new_url)
            if detected:
                update_data["platform"] = detected
        adapter_cls = PlatformFactory.get_platform_class(update_data.get("platform") or existing.platform)
        if adapter_cls is None:
            # 退而用通用正则提取
            import re as _re
            m = _re.search(r'live\.kuaishou\.com/u/(\w+)|live\.kuaishou\.com/(\w+)|live\.douyin\.com/(\d+)|live\.bilibili\.com/(\d+)', new_url)
            update_data["room_id"] = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "") if m else ""
        else:
            update_data["room_id"] = adapter_cls.extract_room_id(new_url)
        # 清空已缓存的平台适配器实例，下次检查按新 URL/平台重建
        # P0-2: 使用 PlatformManager.reset 先 close 旧实例再 clear，避免连接泄漏
        try:
            await monitor._reset_platform_cache()
        except Exception:
            pass

    # 作品订阅开关：开启时必须有 platform_user_id
    if update_data.get("works_enabled"):
        puid = update_data.get("platform_user_id", existing.platform_user_id)
        if not puid:
            raise HTTPException(status_code=400, detail="开启作品订阅需先填写主页地址（或等直播间检测自动识别主播）")

    await db.execute(update(Room).where(Room.id == room_id).values(**update_data))
    await db.commit()

    # 编辑后立即后台重新检测一次，标题/状态/主播名无需等下个监控周期才刷新
    background_tasks.add_task(monitor.check_room_now, room_id)
    # 开启/重新开启作品订阅时触发立即检查（未回填则开始回填）
    if update_data.get("works_enabled"):
        background_tasks.add_task(works_check_task, room_id)

    return {"message": "更新成功"}


@router.delete("/{room_id}")
async def delete_room(room_id: int, db: AsyncSession = Depends(get_db)):
    """删除主播（直播录制记录与作品记录一并删除；已下载文件保留在磁盘）"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="主播不存在")

    # 如果正在录制，先停止
    if room.is_recording:
        await monitor._stop_recording(room)

    # 显式清理从属记录（SQLite 不强制外键，ORM 级联不作用于批量 delete）
    await db.execute(delete(Recording).where(Recording.room_id == room_id))
    await db.execute(delete(Work).where(Work.creator_id == room_id))
    await db.execute(delete(Room).where(Room.id == room_id))
    await db.commit()

    return {"message": "删除成功（已下载文件保留在磁盘）"}


@router.post("/{room_id}/check")
async def check_room(room_id: int, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """手动检查主播直播状态"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="主播不存在")

    background_tasks.add_task(monitor.check_room_now, room_id)

    return {"message": "正在检测..."}


@router.post("/{room_id}/start-recording")
async def manual_start_recording(room_id: int, db: AsyncSession = Depends(get_db)):
    """手动开始录制"""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="主播不存在")

    if room.is_recording:
        raise HTTPException(status_code=400, detail="已在录制中")

    if not room.is_live:
        raise HTTPException(status_code=400, detail="未在直播中")

    # 获取流地址
    platform = await monitor._get_platform(room.platform)
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
        raise HTTPException(status_code=404, detail="主播不存在")

    if not room.is_recording:
        raise HTTPException(status_code=400, detail="未在录制中")

    await monitor._stop_recording(room)

    return {"message": "录制已停止"}


@router.get("/{room_id}/recordings")
async def get_room_recordings(room_id: int, db: AsyncSession = Depends(get_db)):
    """获取主播的录制记录"""
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
