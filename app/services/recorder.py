"""核心录制引擎 - 使用FFmpeg拉流录制"""
import asyncio
import os
import re
import signal
import logging
from typing import Optional
from app.config import settings
from app.utils import localnow

logger = logging.getLogger(__name__)

# P0-3: ffmpeg 的 stdout/stderr 必须持续 drain。asyncio 的 StreamReader 只有被 read
# 时才消费；不读则数据堆在内存里，且管道缓冲（64KB）写满后 ffmpeg 会**阻塞在 write()**
# 上 —— 进程仍存活、is_recording() 仍返回 True、前端显示"录制中"，但文件零字节增长。
# 断流重连时 -loglevel warning 会连续刷警告，长录制必然触发。这里在后台 task 持续消费，
# 只保留尾部若干行用于诊断退出原因，内存有界。
_STDERR_TAIL_LINES = 40
_STDERR_TAIL_BYTES = 8192

# P0-14: 单个 ffmpeg 停止宽限期。SIGTERM 后等这么久，超时再 SIGKILL（再等同样久）。
# 这个值直接决定容器停止预算：docker-compose 的 stop_grace_period 必须 ≥
# (2 × _STOP_GRACE × 并发路数) + 后台任务 8s + 余量。当前 60s 覆盖 5 路并发。
# 调小此值可缩短停机时间；调大则需同步调大 stop_grace_period。
_STOP_GRACE = 5


class _StreamDrainer:
    """持续消费子进程输出流，避免管道写满导致子进程阻塞。

    只保留尾部 N 字节（用于把 ffmpeg 退出原因写进 Recording.error_message），
    其余丢弃，内存有界。
    """

    def __init__(self, stream: Optional[asyncio.StreamReader], label: str):
        self._stream = stream
        self._label = label
        self._tail = b""
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if self._stream is not None:
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                chunk = await self._stream.read(4096)
                if not chunk:
                    return
                self._tail = (self._tail + chunk)[-_STDERR_TAIL_BYTES:]
        except (asyncio.CancelledError, Exception):
            # 进程结束/被 kill 时读取抛异常，属正常收尾，不记 error
            return

    def tail_text(self) -> str:
        """尾部日志（按行截断，便于写入 error_message）"""
        if not self._tail:
            return ""
        lines = self._tail.decode("utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-_STDERR_TAIL_LINES:])

    async def stop(self):
        """等待 drain 收尾（进程已退出时调用），确保尾部数据已读入"""
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None


class FFmpegRecorder:
    """FFmpeg录制器"""

    def __init__(self):
        self.active_processes: dict[int, asyncio.subprocess.Process] = {}
        # 每个房间的 stderr/stdout drainer（P0-3）
        self._drainers: dict[int, _StreamDrainer] = {}
        # 进程退出后保留的最后 stderr 尾部，供 monitor 写入 error_message
        self._last_stderr: dict[int, str] = {}
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
        # 本地时区：文件名/目录名要用主播开播的本地日期，不能是 UTC（P1-25）
        now = localnow()
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

    def build_session_target(self, platform: str, streamer_name: str, room_id: str,
                             base_dir: str, part_index: int = 1,
                             record_format: str = None, segment_time: int = None,
                             template: str = None, title: str = None,
                             remark: str = None) -> tuple[str, str]:
        """为一场所录制的某个 part 计算输出路径。

        base_dir 为该主播的直播间归档目录（archive.live_root），日期层由本方法追加。

        返回 (final_path, part_target)：
        - final_path: 整场录制的最终文件（{base}.{fmt}），断流重连后由所有 part 合并得到。
        - part_target: 本次 ffmpeg 写入目标；segment 模式下包含 %04d 模板。

        文件名 base 由 filename_template 决定；分段仅在 ts/mp4 且 segment_time>0 时生效
        （flv 不支持可靠的流式分段，始终为单文件）。
        """
        fmt = record_format or settings.record_format
        seg = segment_time if segment_time is not None else settings.segment_time

        # 日期目录同样用本地时区（P1-25），与 {date}/{time} 占位符口径一致
        dir_path = os.path.join(base_dir, localnow().strftime("%Y-%m-%d"))
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
                              room_id: str, output_path: str,
                              record_format: str = None,
                              segment_time: int = None) -> dict:
        """开始录制，写入 output_path（由 build_session_target 计算的归档路径）。

        P0-5: "已在录制中"检查 + spawn + 登记必须在**同一把锁**内完成。
        原实现锁只包住检查，spawn 在锁外 —— 监控循环 + API 手动开始 + check_room_now
        三路并发时两路都能通过检查，于是两个 ffmpeg 以 `-y` 写同一 output_path：
        文件被写坏，且后写入的 dict 覆盖前者，前者彻底失联成孤儿进程。
        """
        file_path = output_path
        dir_path = os.path.dirname(output_path)
        seg_time = segment_time if segment_time is not None else settings.segment_time

        cmd = self._build_ffmpeg_command(stream_url, file_path, segment_time=seg_time, record_format=record_format)

        async with self._lock:
            # 检查与登记在同一临界区内，杜绝双 spawn
            if room_db_id in self.active_processes:
                logger.warning(f"房间 {room_db_id} 已在录制中")
                return {"success": False, "error": "已在录制中"}

            logger.info(f"开始录制 房间{room_db_id}: {' '.join(cmd[:6])}... 输出: {file_path}")

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid if os.name != "nt" else None,
                )
            except Exception as e:
                logger.error(f"启动FFmpeg失败: {e}")
                return {"success": False, "error": str(e)}

            self.active_processes[room_db_id] = process
            # P0-3: 立即起 drain task，否则 stderr 管道写满后 ffmpeg 静默卡死
            stderr_drainer = _StreamDrainer(process.stderr, f"room{room_db_id}-stderr")
            stdout_drainer = _StreamDrainer(process.stdout, f"room{room_db_id}-stdout")
            stderr_drainer.start()
            stdout_drainer.start()
            self._drainers[room_db_id] = stderr_drainer
            self._last_stderr.pop(room_db_id, None)

        return {
            "success": True,
            "file_path": file_path,
            "dir_path": dir_path,
            "pid": process.pid,
        }

    async def _collect_tail(self, room_db_id: int) -> str:
        """停止该房间的 drainer 并返回 stderr 尾部（锁外调用，不阻塞临界区）"""
        drainer = self._drainers.pop(room_db_id, None)
        if drainer is None:
            return self._last_stderr.get(room_db_id, "")
        await drainer.stop()
        tail = drainer.tail_text()
        if tail:
            self._last_stderr[room_db_id] = tail
        return tail

    def last_stderr(self, room_db_id: int) -> str:
        """最近一次退出/停止时保留的 ffmpeg stderr 尾部（供写入 error_message）"""
        return self._last_stderr.get(room_db_id, "")

    async def stop_recording(self, room_db_id: int) -> bool:
        """停止录制。

        P0-6: 原实现先把 process 从 active_processes 里 pop 掉，再发 SIGTERM。
        若 kill 失败（killpg 抛非 ProcessLookupError、或 wait 超时后仍未死），
        进程仍在跑但已不在 dict 中 → is_recording() 返回 False → 监控判定"断流"
        并启动新 ffmpeg，旧进程成为**不可管理的孤儿**（cleanup_finished 也看不到）。
        现改为：只在确认 reaped 后才从登记表移除；失败则保留登记并如实返回 False。
        """
        async with self._lock:
            process = self.active_processes.get(room_db_id)

        if process is None:
            # 已无进程登记：仍尝试回收 drainer 残留
            await self._collect_tail(room_db_id)
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
                    await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE)
                except asyncio.TimeoutError:
                    if os.name != "nt":
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    # SIGKILL 后必须 reaped，否则仍是僵尸；给足窗口
                    await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE)

            # 仅在此刻（已确认退出）才从登记表移除，避免留下不可见孤儿
            async with self._lock:
                if self.active_processes.get(room_db_id) is process:
                    self.active_processes.pop(room_db_id, None)

            await self._collect_tail(room_db_id)
            logger.info(f"房间 {room_db_id} 录制已停止")
            return True

        except Exception as e:
            logger.error(f"停止录制失败 房间{room_db_id}（进程仍保留登记以便后续重试）: {e}")
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

    async def cleanup_finished(self):
        """清理已完成的进程（返回房间ID列表）。

        P1-11: 原实现在持锁状态下 `await wait_for(stderr.read(), 2)`，N 个房间同时
        完成即串行阻塞 2N 秒，期间 start/stop/is_recording 全部阻塞（该函数被监控主循环
        直接 await，也阻塞 API 手动开始）。现改为：锁内只收集并移除，锁外收尾。
        """
        finished = []
        async with self._lock:
            for room_id, process in list(self.active_processes.items()):
                if process.returncode is not None:
                    finished.append(room_id)
            for room_id in finished:
                self.active_processes.pop(room_id, None)

        # 锁外收尾：读 stderr 尾部（已由 drainer 持续消费，此处仅取结果）
        for room_id in finished:
            tail = await self._collect_tail(room_id)
            if tail:
                logger.info(f"房间 {room_id} FFmpeg退出输出: {tail[-500:]}")

        return finished


# 全局录制器实例
recorder = FFmpegRecorder()
