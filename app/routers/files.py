"""文件管理API"""
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from app.services.file_manager import file_manager

router = APIRouter(prefix="/api/files", tags=["files"])


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
