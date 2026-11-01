"""合集下载 API

与作品订阅(/api/works)互补：作品订阅按主播周期拉新作品，这里按**一个合集链接**
一次性把该合集全部集数导入 works 表，复用同一套下载队列。

端点：
    POST /api/collections/preview      解析链接 -> 清单预览（不落库）
    POST /api/collections/import       导入入库（可选集号），随后进下载队列
    GET  /api/collections              已导入合集汇总（含各状态计数）
    GET  /api/collections/{key}/items  某合集的全部集与下载状态
    POST /api/collections/{key}/retry  失败集批量重新入队
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Work
from app.services.collection_service import collection_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collections", tags=["collections"])


class PreviewRequest(BaseModel):
    url: str


class ImportRequest(BaseModel):
    url: str
    room_id: Optional[int] = None      # 为空则自动按合集标题创建「作品主播」
    indices: Optional[List[int]] = None  # 只导入这些集号（1 起）；空=全部


def _to_http(e: Exception, status: int = 400):
    """把 service 层的 RuntimeError 统一转成 HTTP 错误"""
    return HTTPException(status_code=status, detail=str(e))


@router.post("/preview")
async def preview(req: PreviewRequest):
    """解析合集链接，返回清单（不落库、不下载）

    用于前端"先看清单再决定下载哪些"。
    """
    try:
        preview = await collection_service.preview(req.url)
    except RuntimeError as e:
        raise _to_http(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("合集解析异常")
        raise HTTPException(status_code=500, detail=f"合集解析失败: {e}")
    return preview.to_dict()


@router.post("/import")
async def import_collection(req: ImportRequest):
    """把合集导入 works 表并加入下载队列（幂等：已存在的集自动跳过）"""
    try:
        result = await collection_service.import_to_works(
            req.url, room_id=req.room_id, indices=req.indices
        )
    except RuntimeError as e:
        raise _to_http(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("合集导入异常")
        raise HTTPException(status_code=500, detail=f"合集导入失败: {e}")

    # 立即踢一次下载队列，不必等下一个轮询周期
    await collection_service.kick_download()
    return result


@router.get("")
async def list_collections():
    """已导入的合集列表（含每集下载进度汇总）"""
    return await collection_service.list_collections()


@router.get("/{collection_key}/items")
async def collection_items(collection_key: str):
    """某个合集下的全部集与各自下载状态"""
    items = await collection_service.collection_items(collection_key)
    if not items:
        raise HTTPException(status_code=404, detail="合集不存在或尚未导入")
    return items


@router.post("/{collection_key}/retry")
async def retry_collection(collection_key: str,
                           db: AsyncSession = Depends(get_db)):
    """把该合集下所有 failed 的集重新置为 pending 并入队"""
    res = await db.execute(
        update(Work)
        .where(
            Work.collection_key == collection_key,
            Work.status == "failed",
        )
        .values(status="pending", error_message=None)
    )
    await db.commit()
    if res.rowcount == 0:
        return {"message": "该合集没有失败的集", "retried": 0}
    await collection_service.kick_download()
    return {"message": f"已重新入队 {res.rowcount} 集", "retried": res.rowcount}
