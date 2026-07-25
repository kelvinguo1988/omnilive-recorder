"""文件管理服务"""
import os
import shutil
import logging
from pathlib import Path
from typing import Optional
from fastapi import HTTPException
from app.config import settings

logger = logging.getLogger(__name__)


class FileManager:
    """录制文件管理"""

    def __init__(self):
        self.output_dir = settings.output_dir

    def get_file_list(self, platform: str = None, streamer: str = None) -> list:
        """获取文件列表"""
        result = []
        base_path = Path(self.output_dir)

        if not base_path.exists():
            return result

        for root, dirs, files in os.walk(base_path):
            for f in files:
                if f.startswith(".") or f in ("README.md", ".gitkeep"):
                    continue

                file_path = os.path.join(root, f)
                rel_path = os.path.relpath(file_path, base_path)

                parts = rel_path.split(os.sep)
                file_platform = parts[0] if len(parts) > 0 else ""
                file_streamer = parts[1] if len(parts) > 1 else ""

                if platform and file_platform != platform:
                    continue
                if streamer and file_streamer != streamer:
                    continue

                stat = os.stat(file_path)
                result.append({
                    "name": f,
                    "path": rel_path,
                    "full_path": file_path,
                    "platform": file_platform,
                    "streamer": file_streamer,
                    "size": stat.st_size,
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "modified_time": stat.st_mtime,
                    "is_video": f.endswith((".ts", ".flv", ".mp4", ".mkv")),
                })

        result.sort(key=lambda x: x["modified_time"], reverse=True)
        return result

    def get_file_path(self, rel_path: str) -> str:
        """获取文件完整路径（安全检查）"""
        base_path = os.path.abspath(self.output_dir)
        full_path = os.path.abspath(os.path.join(base_path, rel_path))

        if not full_path.startswith(base_path):
            raise HTTPException(status_code=403, detail="非法路径访问")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="文件不存在")

        return full_path

    def delete_file(self, rel_path: str) -> bool:
        """删除文件"""
        full_path = self.get_file_path(rel_path)
        try:
            os.remove(full_path)
            logger.info(f"已删除文件: {rel_path}")

            # 清理空目录
            parent = os.path.dirname(full_path)
            while parent and parent.startswith(self.output_dir) and parent != self.output_dir:
                try:
                    if not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
                except OSError:
                    break

            return True
        except Exception as e:
            logger.error(f"删除文件失败: {e}")
            return False

    def get_disk_usage(self) -> dict:
        """获取磁盘使用情况"""
        try:
            usage = shutil.disk_usage(self.output_dir)
            total_gb = round(usage.total / 1024 / 1024 / 1024, 2)
            used_gb = round(usage.used / 1024 / 1024 / 1024, 2)
            free_gb = round(usage.free / 1024 / 1024 / 1024, 2)
            percent = round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0

            # 计算录制目录总大小
            recording_size = 0
            for root, dirs, files in os.walk(self.output_dir):
                for f in files:
                    recording_size += os.path.getsize(os.path.join(root, f))

            return {
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "percent": percent,
                "recording_size_gb": round(recording_size / 1024 / 1024 / 1024, 2),
            }
        except Exception as e:
            logger.error(f"获取磁盘使用情况失败: {e}")
            return {}

    def get_streamers(self) -> list:
        """获取所有主播列表"""
        result = []
        base_path = Path(self.output_dir)

        if not base_path.exists():
            return result

        for platform_dir in base_path.iterdir():
            if not platform_dir.is_dir():
                continue

            for streamer_dir in platform_dir.iterdir():
                if not streamer_dir.is_dir():
                    continue

                total_size = 0
                file_count = 0
                for root, dirs, files in os.walk(streamer_dir):
                    for f in files:
                        if not f.startswith("."):
                            total_size += os.path.getsize(os.path.join(root, f))
                            file_count += 1

                if file_count > 0:
                    result.append({
                        "platform": platform_dir.name,
                        "streamer": streamer_dir.name,
                        "file_count": file_count,
                        "total_size_mb": round(total_size / 1024 / 1024, 2),
                        "total_size_gb": round(total_size / 1024 / 1024 / 1024, 2),
                    })

        result.sort(key=lambda x: x["total_size_mb"], reverse=True)
        return result


file_manager = FileManager()
