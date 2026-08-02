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

    def _build_base_name(self, platform: str, streamer_name: str, room_id: str,
                         template: str = None, title: str = None, remark: str = None) -> str:
        """根据文件名模板生成基础文件名（不含扩展名）。

        支持的占位符：
          {streamer}  主播名（清洗后，无则回退 room_id）
          {room_id}   房间 ID
          {platform}  平台中文名（抖音/B站/快手）
          {title}     直播标题（清洗后）
          {remark}    添加房间时填写的备注（无则留空）
          {date}      日期 YYYY-MM-DD
          {time}      时间 HHMMSS
          {datetime}  日期时间 YYYYMMDD_HHMMSS
        """
        now = datetime.now()
        streamer = self._sanitize_filename(streamer_name) or (self._sanitize_filename(room_id) or "untitled")
        platform_cn = {
            "douyin": "抖音",
            "bilibili": "B站",
            "kuaishou": "快手",
        }.get(platform, platform)

        mapping = {
            "streamer": streamer,
            "room_id": self._sanitize_filename(room_id) if room_id else "",
            "platform": platform_cn,
            "title": self._sanitize_filename(title) if title else "",
            "remark": self._sanitize_filename(remark) if remark else "",
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H%M%S"),
            "datetime": now.strftime("%Y%m%d_%H%M%S"),
        }

        tmpl = template or settings.filename_template or "{streamer}_{time}"
        base = tmpl
        for k, v in mapping.items():
            base = base.replace("{" + k + "}", v)

        # 整体再清洗一次（去掉模板可能引入的非法字符，并限长）
        base = self._sanitize_filename(base)
        return base or "untitled"

    def _get_output_path(self, platform: str, streamer_name: str, room_id: str,
                         record_format: str = None, template: str = None,
                         title: str = None, remark: str = None) -> tuple[str, str]:
        """获取输出文件路径（自动命名，用于未显式指定 output_path 时的兜底）"""
        fmt = record_format or settings.record_format
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")

        streamer = self._sanitize_filename(streamer_name) or room_id
        platform_cn = {
            "douyin": "抖音",
            "bilibili": "B站",
            "kuaishou": "快手",
        }.get(platform, platform)

        dir_path = os.path.join(settings.output_dir, platform_cn, streamer, date_str)
        os.makedirs(dir_path, exist_ok=True)

        base = self._build_base_name(platform, streamer_name, room_id, template=template, title=title, remark=remark)
        filename = f"{base}.{fmt}"
        file_path = os.path.join(dir_path, filename)

        return dir_path, file_path

    def build_session_target(self, platform: str, streamer_name: str, room_id: str,
                             part_index: int = 1, record_format: str = None,
                             segment_time: int = None, template: str = None,
                             title: str = None, remark: str = None) -> tuple[str, str]:
        """为一场所录制的某个 part 计算输出路径。

        返回 (final_path, part_target)：
        - final_path: 整场录制的最终文件（{base}.{fmt}），断流重连后由所有 part 合并得到。
        - part_target: 本次 ffmpeg 写入目标；segment 模式下包含 %04d 模板。

        文件名 base 由 filename_template 决定；分段仅在 ts/mp4 且 segment_time>0 时生效
        （flv 不支持可靠的流式分段，始终为单文件）。
        """
        fmt = record_format or settings.record_format
        seg = segment_time if segment_time is not None else settings.segment_time

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")

        streamer = self._sanitize_filename(streamer_name) or room_id
        platform_cn = {
            "douyin": "抖音",
            "bilibili": "B站",
            "kuaishou": "快手",
        }.get(platform, platform)

        dir_path = os.path.join(settings.output_dir, platform_cn, streamer, date_str)
        os.makedirs(dir_path, exist_ok=True)

        base = self._build_base_name(platform, streamer_name, room_id, template=template, title=title, remark=remark)
        final_path = os.path.join(dir_path, f"{base}.{fmt}")
        if seg and seg > 0 and fmt in ("ts", "mp4"):
            part_target = os.path.join(dir_path, f"{base}_part{part_index:03d}_%04d.{fmt}")
        else:
            part_target = os.path.join(dir_path, f"{base}_part{part_index:03d}.{fmt}")

        return final_path, part_target

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
            if seg_time and seg_time > 0:
                # 若调用方已提供分段模板（含 %04d，如断流重连的 part），直接使用；
                # 否则基于输出路径自动追加 _%04d 分段后缀
                seg_target = output_path
                if "%04d" not in output_path:
                    seg_target = output_path.replace(f".{fmt}", f"_%04d.{fmt}")
                cmd.extend(["-f", "segment",
                            "-segment_format", "mpegts",
                            "-reset_timestamps", "1",
                            "-segment_time", str(seg_time),
                            seg_target])
            else:
                cmd.extend(["-f", "mpegts", output_path])
        elif fmt == "flv":
            # flv 不支持可靠的流式分段，始终单文件
            cmd.extend(["-f", "flv", output_path])
        elif fmt == "mp4":
            if seg_time and seg_time > 0 and "%04d" in output_path:
                # mp4 分段：每个分片为独立可播放的 mp4，断流重连时由 merge 合并
                cmd.extend(["-f", "segment",
                            "-segment_format", "mp4",
                            "-reset_timestamps", "1",
                            "-segment_time", str(seg_time),
                            output_path])
            else:
                cmd.extend(["-f", "mp4",
                            "-movflags", "+faststart",
                            output_path])
        else:
            cmd.append(output_path)

        return cmd

    async def start_recording(self, room_db_id: int, stream_url: str,
                              platform: str, streamer_name: str,
                              room_id: str, record_format: str = None,
                              output_path: str = None,
                              segment_time: int = None) -> dict:
        """开始录制。

        output_path 指定时写入该路径（用于断流重连续写同一场的某个 part），
        否则按默认命名自动生成（开播 / 手动开始时用于首个 part）。
        """
        async with self._lock:
            if room_db_id in self.active_processes:
                logger.warning(f"房间 {room_db_id} 已在录制中")
                return {"success": False, "error": "已在录制中"}

        if output_path is None:
            dir_path, file_path = self._get_output_path(
                platform, streamer_name, room_id, record_format
            )
            seg_time = segment_time if segment_time is not None else settings.segment_time
        else:
            file_path = output_path
            dir_path = os.path.dirname(output_path)
            seg_time = segment_time if segment_time is not None else settings.segment_time

        cmd = self._build_ffmpeg_command(stream_url, file_path, segment_time=seg_time, record_format=record_format)
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
