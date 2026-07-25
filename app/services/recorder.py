"""核心录制引擎 - 使用FFmpeg拉流录制"""
import asyncio
import os
import re
import time
import signal
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


class FFmpegRecorder:
    """FFmpeg录制器"""

    def __init__(self):
        self.active_processes: dict[int, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    def _sanitize_filename(self, name: str) -> str:
        """清理文件名中的非法字符"""
        if not name:
            return "untitled"
        name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
        name = name.strip().strip(".")
        return name[:100] if name else "untitled"

    def _get_output_path(self, platform: str, streamer_name: str, room_id: str,
                         record_format: str = None) -> tuple[str, str]:
        """获取输出文件路径"""
        fmt = record_format or settings.record_format
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")

        streamer = self._sanitize_filename(streamer_name) or room_id
        platform_cn = {
            "douyin": "抖音",
            "bilibili": "B站",
            "kuaishou": "快手",
        }.get(platform, platform)

        dir_path = os.path.join(settings.output_dir, platform_cn, streamer, date_str)
        os.makedirs(dir_path, exist_ok=True)

        filename = f"{streamer}_{time_str}.{fmt}"
        file_path = os.path.join(dir_path, filename)

        return dir_path, file_path

    def _build_ffmpeg_command(self, stream_url: str, output_path: str,
                              segment_time: int = None, record_format: str = None) -> list:
        """构建FFmpeg命令"""
        fmt = record_format or settings.record_format
        seg_time = segment_time or settings.segment_time

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-rw_timeout", "15000000",
            "-thread_queue_size", "512",
        ]

        # 输入选项
        if ".m3u8" in stream_url:
            cmd.extend(["-protocol_whitelist", "tcp,https,crypto,file",
                        "-allowed_extensions", "ALL",
                        "-i", stream_url])
        elif ".flv" in stream_url:
            cmd.extend(["-timeout", "15",
                        "-i", stream_url])
        else:
            cmd.extend(["-i", stream_url])

        # 编码选项 - 直接拷贝流
        cmd.extend(["-c", "copy"])

        # 输出格式
        if fmt == "ts":
            cmd.extend(["-f", "mpegts"])
            if seg_time > 0:
                cmd.extend(["-segment_time", str(seg_time),
                            "-segment_format", "mpegts",
                            "-reset_timestamps", "1",
                            "-f", "segment",
                            output_path.replace(f".{fmt}", "_%04d.ts")])
            else:
                cmd.append(output_path)
        elif fmt == "flv":
            cmd.extend(["-f", "flv"])
            cmd.append(output_path)
        elif fmt == "mp4":
            cmd.extend(["-f", "mp4",
                        "-movflags", "+faststart"])
            cmd.append(output_path)
        else:
            cmd.append(output_path)

        return cmd

    async def start_recording(self, room_db_id: int, stream_url: str,
                              platform: str, streamer_name: str,
                              room_id: str, record_format: str = None) -> dict:
        """开始录制"""
        async with self._lock:
            if room_db_id in self.active_processes:
                logger.warning(f"房间 {room_db_id} 已在录制中")
                return {"success": False, "error": "已在录制中"}

        dir_path, file_path = self._get_output_path(
            platform, streamer_name, room_id, record_format
        )

        cmd = self._build_ffmpeg_command(stream_url, file_path, record_format=record_format)
        logger.info(f"开始录制 房间{room_db_id}: {' '.join(cmd[:6])}... 输出: {file_path}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid if os.name != "nt" else None,
            )

            async with self._lock:
                self.active_processes[room_db_id] = process

            return {
                "success": True,
                "file_path": file_path,
                "dir_path": dir_path,
                "pid": process.pid,
            }

        except Exception as e:
            logger.error(f"启动FFmpeg失败: {e}")
            return {"success": False, "error": str(e)}

    async def stop_recording(self, room_db_id: int) -> bool:
        """停止录制"""
        async with self._lock:
            process = self.active_processes.pop(room_db_id, None)

        if process is None:
            return False

        try:
            if process.returncode is None:
                if os.name != "nt":
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    process.terminate()

                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    if os.name != "nt":
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    await process.wait()

            logger.info(f"房间 {room_db_id} 录制已停止")
            return True

        except Exception as e:
            logger.error(f"停止录制失败 房间{room_db_id}: {e}")
            return False

    async def is_recording(self, room_db_id: int) -> bool:
        """检查是否正在录制"""
        async with self._lock:
            process = self.active_processes.get(room_db_id)
            if process is None:
                return False
            return process.returncode is None

    async def get_recording_pid(self, room_db_id: int) -> Optional[int]:
        """获取录制进程PID"""
        async with self._lock:
            process = self.active_processes.get(room_db_id)
            return process.pid if process and process.returncode is None else None

    async def get_file_size(self, file_path: str) -> int:
        """获取文件大小"""
        try:
            if os.path.exists(file_path):
                return os.path.getsize(file_path)
            # 检查分段文件
            pattern = file_path.replace(".ts", "_*.ts")
            import glob
            total = 0
            for f in glob.glob(pattern):
                total += os.path.getsize(f)
            return total
        except Exception:
            return 0

    async def cleanup_finished(self):
        """清理已完成的进程"""
        async with self._lock:
            finished = []
            for room_id, process in self.active_processes.items():
                if process.returncode is not None:
                    finished.append(room_id)

            for room_id in finished:
                process = self.active_processes.pop(room_id)
                stderr = b""
                if process.stderr:
                    try:
                        stderr = await asyncio.wait_for(process.stderr.read(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                if stderr:
                    stderr_text = stderr.decode("utf-8", errors="ignore")[-500:]
                    logger.info(f"房间 {room_id} FFmpeg输出: {stderr_text}")

            return finished


# 全局录制器实例
recorder = FFmpegRecorder()
