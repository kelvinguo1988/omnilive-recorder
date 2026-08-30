"""作品订阅API - 创作者管理与作品下载"""
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import Creator, Work
from app.services.platform import PlatformFactory
from app.services.works_monitor import works_monitor, PLATFORM_CN

router = APIRouter(prefix="/api/works", tags=["works"])


class CreatorCreate(BaseModel):
    home_url: str
    platform: Optional[str] = None
    remark: Optional[str] = None
    enabled: bool = True


class CreatorUpdate(BaseModel):
    enabled: Optional[bool] = None
    remark: Optional[str] = None
    nickname: Optional[str] = None


@router.get("/creators")
async def list_creators(db: AsyncSession = Depends(get_db)):
    """创作者列表（含作品统计）"""
    result = await db.execute(select(Creator).order_by(Creator.id.desc()))
    creators = result.scalars().all()

    # 一次性聚合每个创作者的作品统计
    stats = {}
    rows = (await db.execute(
        select(
            Work.creator_id,
            func.count(Work.id),
            func.sum(func.coalesce(Work.file_size, 0)),
            func.max(Work.publish_time),
        )
        .group_by(Work.creator_id)
    )).all()
    for creator_id, total, size, latest in rows:
        stats[creator_id] = {"total": total, "size": size or 0, "latest": latest}

    status_rows = (await db.execute(
        select(Work.creator_id, Work.status, func.count(Work.id))
        .group_by(Work.creator_id, Work.status)
    )).all()
    for creator_id, status, cnt in status_rows:
        stats.setdefault(creator_id, {"total": 0, "size": 0, "latest": None})
        stats[creator_id][f"{status}_count"] = cnt

    return [
        {
            "id": c.id,
            "platform": c.platform,
            "platform_name": PLATFORM_CN.get(c.platform, c.platform),
            "platform_user_id": c.platform_user_id,
            "nickname": c.nickname,
            "avatar_url": c.avatar_url,
            "home_url": c.home_url,
            "enabled": c.enabled,
            "backfill_done": c.backfill_done,
            "remark": c.remark,
            "last_check_time": c.last_check_time.isoformat() if c.last_check_time else None,
            "work_total": stats.get(c.id, {}).get("total", 0),
            "completed_count": stats.get(c.id, {}).get("completed_count", 0),
            "pending_count": stats.get(c.id, {}).get("pending_count", 0),
            "failed_count": stats.get(c.id, {}).get("failed_count", 0),
            "total_size_mb": round(stats.get(c.id, {}).get("size", 0) / 1024 / 1024, 2),
            "latest_publish_time": stats.get(c.id, {}).get("latest").isoformat()
                if stats.get(c.id, {}).get("latest") else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in creators
    ]


@router.post("/creators")
async def add_creator(body: CreatorCreate, background_tasks: BackgroundTasks,
                      db: AsyncSession = Depends(get_db)):
    """添加创作者（粘贴主页链接，自动识别平台并解析昵称）"""
    url = (body.home_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请输入创作者主页链接")

    platform = body.platform
    if not platform:
        platform = PlatformFactory.detect_platform(url)
        if not platform:
            raise HTTPException(status_code=400, detail="无法识别平台（douyin/bilibili/kuaishou）")

    cls = PlatformFactory.get_platform_class(platform)
    user_id = cls.extract_user_id(url) if cls else ""
    if not user_id:
        hints = {
            "douyin": "抖音需形如 https://www.douyin.com/user/MS4wLjABAAAA...",
            "bilibili": "B站需形如 https://space.bilibili.com/123456789",
            "kuaishou": "快手需形如 https://www.kuaishou.com/profile/xxx",
        }
        raise HTTPException(status_code=400, detail=f"无法从链接解析用户ID。{hints.get(platform, '')}")

    existing = await db.execute(
        select(Creator).where(
            Creator.platform == platform, Creator.platform_user_id == user_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该创作者已存在")

    # 解析昵称（失败不阻塞添加，WorksMonitor 稍后补齐）
    nickname, avatar = None, None
    try:
        adapter = await works_monitor._get_platform(platform)
        if adapter:
            info = await adapter.get_user_info(user_id)
            nickname = info.nickname or None
            avatar = info.avatar_url or None
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"添加创作者时解析昵称失败: {e}")

    creator = Creator(
        platform=platform,
        platform_user_id=user_id,
        nickname=nickname,
        avatar_url=avatar,
        home_url=url,
        enabled=body.enabled,
        remark=body.remark,
    )
    db.add(creator)
    await db.commit()
    await db.refresh(creator)

    # 后台立即首检（触发全量回填）
    background_tasks.add_task(works_monitor.check_creator_by_id, creator.id)

    return {
        "id": creator.id,
        "platform": platform,
        "user_id": user_id,
        "nickname": nickname,
        "message": "添加成功，正在后台回填历史作品..." if body.enabled else "添加成功（已禁用，不会拉取作品）",
    }


@router.put("/creators/{creator_id}")
async def update_creator(creator_id: int, body: CreatorUpdate,
                         db: AsyncSession = Depends(get_db)):
    """更新创作者（启用/禁用、备注、手动改昵称用于目录命名）"""
    result = await db.execute(select(Creator).where(Creator.id == creator_id))
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="创作者不存在")

    from sqlalchemy import update as sa_update
    values = {}
    if body.enabled is not None:
        values["enabled"] = body.enabled
    if body.remark is not None:
        values["remark"] = body.remark.strip()
    if body.nickname is not None:
        values["nickname"] = body.nickname.strip() or None
    if not values:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    await db.execute(sa_update(Creator).where(Creator.id == creator_id).values(**values))
    await db.commit()
    return {"message": "更新成功"}


@router.delete("/creators/{creator_id}")
async def delete_creator(creator_id: int, db: AsyncSession = Depends(get_db)):
    """删除创作者（连同作品记录；已下载文件保留在磁盘）"""
    result = await db.execute(select(Creator).where(Creator.id == creator_id))
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="创作者不存在")

    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(Work).where(Work.creator_id == creator_id))
    await db.execute(sa_delete(Creator).where(Creator.id == creator_id))
    await db.commit()
    return {"message": "删除成功（已下载文件保留在磁盘）"}


@router.post("/creators/{creator_id}/check")
async def check_creator_now(creator_id: int, background_tasks: BackgroundTasks,
                            db: AsyncSession = Depends(get_db)):
    """立即检查创作者作品"""
    result = await db.execute(select(Creator.id).where(Creator.id == creator_id))
    if not result.first():
        raise HTTPException(status_code=404, detail="创作者不存在")
    background_tasks.add_task(works_monitor.check_creator_by_id, creator_id)
    return {"message": "正在检查..."}


@router.get("")
async def list_works(creator_id: int = None, status: str = None, limit: int = 200,
                     db: AsyncSession = Depends(get_db)):
    """作品列表（可按创作者/状态过滤）"""
    query = (
        select(Work, Creator)
        .join(Creator, Work.creator_id == Creator.id)
        .order_by(Work.publish_time.desc().nullslast(), Work.id.desc())
        .limit(min(limit, 500))
    )
    if creator_id is not None:
        query = query.where(Work.creator_id == creator_id)
    if status:
        query = query.where(Work.status == status)

    rows = (await db.execute(query)).all()
    return [
        {
            "id": w.id,
            "creator_id": w.creator_id,
            "nickname": c.nickname,
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
