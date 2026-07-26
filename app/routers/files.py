"""文件管理API"""
import asyncio
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from app.services.file_manager import file_manager

router = APIRouter(prefix="/api/files", tags=["files"])


class MergeRequest(BaseModel):
    file_paths: list[str]
    output_format: str = "mp4"


class BatchDeleteRequest(BaseModel):
    file_paths: list[str]


@router.get("")
async def list_files(platform: str = None, streamer: str = None):
    """获取文件列表"""
    return file_manager.get_file_list(platform=platform, streamer=streamer)


@router.get("/streamers")
async def list_streamers():
    """获取主播列表（按文件统计）"""
    return file_manager.get_streamers()


@router.get("/download/{file_path:path}")
async def download_file(file_path: str):
    """下载文件"""
    full_path = file_manager.get_file_path(file_path)
    filename = os.path.basename(full_path)
    return FileResponse(
        full_path,
        media_type="application/octet-stream",
        filename=filename,
    )


@router.get("/play/{file_path:path}")
async def play_file(file_path: str):
    """在线播放文件"""
    full_path = file_manager.get_file_path(file_path)

    ext = os.path.splitext(full_path)[1].lower()
    media_types = {
        ".ts": "video/mp2t",
        ".flv": "video/x-flv",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    file_size = os.path.getsize(full_path)

    def iter_file():
        with open(full_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=media_type,
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"inline; filename=\"{os.path.basename(full_path)}\"",
        },
    )


@router.delete("/{file_path:path}")
async def delete_file(file_path: str):
    """删除文件"""
    success = file_manager.delete_file(file_path)
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")
    return {"message": "删除成功"}


@router.post("/merge")
async def merge_files(req: MergeRequest):
    """合并多个碎片录制文件为一个 (concat demuxer, 流拷贝不重编码)"""
    if not req.file_paths or len(req.file_paths) < 2:
        raise HTTPException(status_code=400, detail="请至少选择 2 个文件")
    result = await asyncio.to_thread(
        file_manager.merge_recordings, req.file_paths, req.output_format
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "合并失败"))
    return result


@router.post("/batch-delete")
async def batch_delete_files(req: BatchDeleteRequest):
    """批量删除文件（逐条复用 delete_file，含路径安全校验）"""
    if not req.file_paths:
        raise HTTPException(status_code=400, detail="未提供要删除的文件")

    deleted, failed = [], []
    for rel in req.file_paths:
        try:
            ok = file_manager.delete_file(rel)
            if ok:
                deleted.append(rel)
            else:
                failed.append({"path": rel, "error": "删除失败"})
        except HTTPException as he:
            failed.append({"path": rel, "error": he.detail})
        except Exception as e:  # noqa: BLE001
            failed.append({"path": rel, "error": str(e)})

    return {
        "success": len(failed) == 0,
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
    }
