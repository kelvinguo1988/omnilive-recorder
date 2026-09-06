"""作品订阅监控 - 定时检测主播新作品并自动下载

与直播监控(monitor)平行的独立领域模块，仅通过 platform_manager 共享适配器实例：
- 每 works_poll_interval 检查所有开启作品订阅的主播（rooms.works_enabled）
- 主播的 platform_user_id 是订阅依据：主页地址解析得到，或直播间检测自动回填
- 首次订阅全量回填历史作品（works_backfill_limit 可限制条数）
- 新作品入库 status=pending，由下载队列按平台串行下载（随机间隔限速防风控）

风控应对原则：请求签名(a_bogus/wbi)、登录态Cookie、随机化间隔、单平台并发=1、
失败退避。不做验证码破解/代理池——目标是像正常访问一样不触发风控。
"""
import asyncio
import io
import json
import os
import random
import time
import zipfile
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select, update

from app.database import async_session
from app.models import Room, Work
from app.config import settings
from app.services.platform.base import WorkInfo, WORKS_UA, PLATFORM_CN
from app.services.platform_manager import platform_manager
from app.services.recorder import recorder

logger = logging.getLogger(__name__)

# 各平台下载 CDN 的请求头：B站/快手 CDN 校验 Referer，抖音 CDN 校验 UA 一致性
DOWNLOAD_HEADERS = {
    "douyin": {"User-Agent": WORKS_UA, "Referer": "https://www.douyin.com/"},
    "bilibili": {"User-Agent": WORKS_UA, "Referer": "https://www.bilibili.com/"},
    "kuaishou": {"User-Agent": WORKS_UA, "Referer": "https://www.kuaishou.com/"},
}

# 平台间请求随机间隔（秒）：作品列表拉取后
CHECK_JITTER = (2.0, 6.0)
# 同平台相邻下载的最小间隔（秒）
DOWNLOAD_GAP = (3.0, 7.0)


class WorksMonitor:
    """作品订阅监控调度器"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 每平台下载并发=1，避免同一平台并发请求触发风控
        self._download_sems: dict = {p: asyncio.Semaphore(1) for p in PLATFORM_CN}
        self._last_download_ts: dict = {}
        # 重入保护：API 触发的立即检查与循环检查不并发执行
        self._check_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def start(self):
        if self._running:
            return
        self._running = True
        await self._recover_stale_works()
        self._task = asyncio.create_task(self._loop())
        logger.info("作品订阅监控已启动")

    async def _recover_stale_works(self):
        """启动时恢复上次异常退出遗留的 downloading 作品（对标直播录制的 P0-3 修复）。

        服务被强杀重启后，status=downloading 的作品已无下载进程，若不恢复
        会永远卡在下载中。回到 pending 让队列重新拾取。
        """
        try:
            async with async_session() as session:
                res = await session.execute(
                    update(Work).where(Work.status == "downloading").values(
                        status="pending",
                        error_message="服务重启恢复：下载中断，回到待下载队列",
                    )
                )
                if res.rowcount:
                    await session.commit()
                    logger.warning(f"恢复 {res.rowcount} 条遗留 downloading 作品为待下载")
        except Exception as e:
            logger.error(f"恢复遗留 downloading 作品失败: {e}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        dl_client = getattr(self, "_dl_client", None)
        if dl_client is not None:
            try:
                await dl_client.aclose()
            except Exception:
                pass
            self._dl_client = None
        logger.info("作品订阅监控已停止")

    async def _loop(self):
        """检查与下载双循环并行：全量回填可能持续数小时（逐页防风控延迟），
        若串行执行会让下载饥饿——回填的作品全部停在待下载。下载走平台 CDN、
        检查走平台 API，两者各自限速互不抢占，可安全并行。"""
        # 启动后稍等，避免与直播监控/应用启动争抢
        await asyncio.sleep(5)
        check_task = asyncio.create_task(self._check_loop())
        download_task = asyncio.create_task(self._download_loop())
        try:
            await asyncio.gather(check_task, download_task)
        finally:
            for t in (check_task, download_task):
                t.cancel()
            await asyncio.gather(check_task, download_task, return_exceptions=True)

    async def _check_loop(self):
        while self._running:
            try:
                await self.check_all_rooms()
            except Exception as e:
                logger.error(f"作品检查循环异常: {e}")
            interval = settings.works_poll_interval
            # 分段 sleep，保证 stop() 能及时退出
            waited = 0.0
            while self._running and waited < interval:
                step = min(5.0, interval - waited)
                await asyncio.sleep(step)
                waited += step

    async def _download_loop(self):
        # 下载拾取间隔短（作品入库后尽快开始下载），单轮内仍有平台串行+3~7s限速
        while self._running:
            try:
                await self.process_download_queue()
            except Exception as e:
                logger.error(f"作品下载队列异常: {e}")
            waited = 0.0
            while self._running and waited < 10:
                await asyncio.sleep(2)
                waited += 2

    # ---------- 检查 ----------

    async def check_all_rooms(self):
        """检查所有开启作品订阅且可用的主播"""
        async with async_session() as session:
            result = await session.execute(
                select(Room).where(
                    Room.enabled == True,
                    Room.works_enabled == True,
                    Room.platform_user_id != None,
                    Room.platform_user_id != "",
                )
            )
            rooms = result.scalars().all()
        for room in rooms:
            if not self._running:
                break
            try:
                await self.check_room_works(room)
            except Exception as e:
                logger.error(f"检查主播作品 {room.streamer_name or room.id} 失败: {e}")
            await asyncio.sleep(random.uniform(*CHECK_JITTER))

    async def check_room_works_by_id(self, room_id: int):
        """API 触发的立即检查（按 id 重读主播）"""
        async with async_session() as session:
            result = await session.execute(select(Room).where(Room.id == room_id))
            room = result.scalar_one_or_none()
        if room:
            await self.check_room_works(room)

    async def check_room_works(self, room: Room):
        """检查单个主播的作品：未回填则全量回填，否则增量拉最新一页"""
        if not room.platform_user_id:
            return
        async with self._check_lock:
            await self._check_room_works_inner(room)

    async def _check_room_works_inner(self, room: Room):
        adapter = await platform_manager.get(room.platform)
        if not adapter:
            logger.warning(f"作品订阅不支持的平台: {room.platform}")
            return

        # 主播名为空时补一次用户信息（添加时网络失败的可能已恢复）
        if not room.streamer_name:
            try:
                info = await adapter.get_user_info(room.platform_user_id)
            except Exception as e:
                info = None
                logger.warning(f"获取主播昵称失败 {room.platform_user_id}: {e}")
            if info and info.nickname:
                async with async_session() as session:
                    await session.execute(
                        update(Room).where(Room.id == room.id).values(
                            streamer_name=info.nickname,
                        )
                    )
                    await session.commit()
                room.streamer_name = info.nickname

        added = 0
        try:
            if not room.backfill_done:
                added = await self._backfill(room, adapter)
            else:
                works, _, _ = await adapter.get_user_works(
                    room.platform_user_id, 0, settings.works_check_count
                )
                added = await self._insert_new_works(room.id, works)
        except Exception as e:
            # 拉取失败（风控/网络/Cookie）：保留 backfill_done=False，下个周期自动重试
            logger.warning(
                f"作品拉取失败 [{PLATFORM_CN.get(room.platform, room.platform)}] "
                f"{room.streamer_name or room.platform_user_id}: {e}"
            )

        now = datetime.utcnow()
        async with async_session() as session:
            await session.execute(
                update(Room).where(Room.id == room.id).values(last_work_check_time=now)
            )
            await session.commit()

        label = room.streamer_name or room.platform_user_id
        logger.info(f"作品检查完成 [{PLATFORM_CN.get(room.platform, room.platform)}] {label}: 新增 {added} 条")

    async def _backfill(self, room: Room, adapter) -> int:
        """全量回填历史作品（翻页直到没有更多），works_backfill_limit 限制条数"""
        limit = settings.works_backfill_limit
        cursor = 0
        total = 0
        page_guard = 0
        truncated = False
        label = room.streamer_name or room.platform_user_id
        while True:
            works, next_cursor, has_more = await adapter.get_user_works(
                room.platform_user_id, cursor, settings.works_check_count
            )
            # 上限截断：只入库剩余配额内的作品
            if limit and total + len(works) > limit:
                works = works[: max(limit - total, 0)]
                truncated = True
            total += await self._insert_new_works(room.id, works)
            page_guard += 1
            if limit and total >= limit:
                logger.info(f"主播 {label} 回填达到上限 {limit} 条，截断")
                break
            if truncated or not has_more or not works or page_guard >= 500:
                break
            cursor = next_cursor
            await asyncio.sleep(random.uniform(2.0, 5.0))

        async with async_session() as session:
            await session.execute(
                update(Room).where(Room.id == room.id).values(backfill_done=True)
            )
            await session.commit()
        logger.info(f"主播 {label} 历史作品回填完成，共 {total} 条")
        return total

    async def _insert_new_works(self, room_id: int, works: list) -> int:
        """作品入库（查重+唯一约束兜底），返回新增条数"""
        added = 0
        async with async_session() as session:
            for w in works:
                exists = await session.execute(
                    select(Work.id).where(
                        Work.creator_id == room_id,
                        Work.platform_work_id == w.work_id,
                    )
                )
                if exists.first():
                    continue
                session.add(Work(
                    creator_id=room_id,
                    platform_work_id=w.work_id,
                    work_type=w.work_type,
                    title=(w.title or "")[:500],
                    publish_time=datetime.fromtimestamp(w.publish_ts).replace(tzinfo=None) if w.publish_ts else None,
                    duration=w.duration,
                    download_urls=json.dumps(w.download_urls, ensure_ascii=False) if w.download_urls else None,
                    status="pending",
                ))
                added += 1
            if added:
                await session.commit()
        return added

    # ---------- 下载 ----------

    async def process_download_queue(self):
        """处理待下载作品（按平台串行 + 随机间隔限速）"""
        if not settings.works_auto_download:
            return
        async with async_session() as session:
            result = await session.execute(
                select(Work).where(Work.status == "pending").order_by(Work.id).limit(100)
            )
            pendings = result.scalars().all()
        if not pendings:
            return

        room_cache: dict = {}
        for work in pendings:
            if not self._running:
                break
            if work.creator_id not in room_cache:
                async with async_session() as session:
                    res = await session.execute(
                        select(Room).where(Room.id == work.creator_id)
                    )
                    room_cache[work.creator_id] = res.scalar_one_or_none()
            room = room_cache[work.creator_id]
            if not room or not room.enabled or not room.works_enabled:
                continue

            platform = room.platform
            async with self._download_sems[platform]:
                # 双触发源（循环+重试端点）并发时，进信号量后重查状态，
                # 已被另一个任务处理过（downloading/completed/failed）的跳过
                async with async_session() as session:
                    fresh = await session.get(Work, work.id)
                if fresh is None or fresh.status != "pending":
                    continue
                work = fresh
                # 平台级随机限速
                gap = random.uniform(*DOWNLOAD_GAP)
                wait = self._last_download_ts.get(platform, 0) + gap - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    await self._download_work(room, work)
                except Exception as e:
                    logger.error(f"作品下载失败 [{platform}] {work.platform_work_id}: {e}")
                    async with async_session() as session:
                        await session.execute(
                            update(Work).where(Work.id == work.id).values(
                                status="failed", error_message=str(e)[:500],
                            )
                        )
                        await session.commit()
                finally:
                    self._last_download_ts[platform] = time.time()

    async def _download_work(self, room: Room, work: Work):
        """下载单个作品（视频→单文件；图集→打包 zip），.part 临时文件原子改名"""
        adapter = await platform_manager.get(room.platform)
        if not adapter:
            raise RuntimeError("平台适配器不可用")

        async with async_session() as session:
            await session.execute(
                update(Work).where(Work.id == work.id).values(status="downloading")
            )
            await session.commit()

        # 解析直链：优先列表时缓存的地址，否则二次解析（B站）
        urls = []
        if work.download_urls:
            try:
                urls = json.loads(work.download_urls)
            except (ValueError, TypeError):
                urls = []
        if not urls:
            info = WorkInfo(work_id=work.platform_work_id, work_type=work.work_type,
                            title=work.title or "")
            urls = await adapter.get_download_urls(info)
        if not urls:
            raise RuntimeError("未能获取下载地址（可能被风控或作品已删除）")

        dir_path = os.path.join(
            settings.output_dir, "works",
            PLATFORM_CN.get(room.platform, room.platform),
            recorder._sanitize_filename(room.streamer_name or room.platform_user_id),
        )
        os.makedirs(dir_path, exist_ok=True)

        date_str = work.publish_time.strftime("%Y%m%d") if work.publish_time else \
            datetime.now().strftime("%Y%m%d")
        title = recorder._sanitize_filename(work.title or "")[:60] or "untitled"
        base = f"{date_str}_{title}_{work.platform_work_id}"

        if work.work_type == "images":
            final_path = os.path.join(dir_path, f"{base}.zip")
            size = await self._download_images(urls, final_path)
        else:
            ext = ".mp4"
            for u in urls[:1]:
                tail = urlparse(u).path.lower()
                for candidate in (".flv", ".ts", ".mkv"):
                    if tail.endswith(candidate):
                        ext = candidate
            final_path = os.path.join(dir_path, f"{base}{ext}")
            size = await self._download_video(urls, final_path)

        async with async_session() as session:
            await session.execute(
                update(Work).where(Work.id == work.id).values(
                    status="completed",
                    file_path=os.path.relpath(final_path, settings.output_dir),
                    file_size=size,
                    error_message=None,
                    downloaded_at=datetime.utcnow(),
                )
            )
            await session.commit()

        label = room.streamer_name or room.platform_user_id
        logger.info(
            f"作品下载完成 [{PLATFORM_CN.get(room.platform, room.platform)}] "
            f"{label}: {os.path.basename(final_path)} ({round(size / 1024 / 1024, 2)} MB)"
        )

    async def _download_video(self, urls: list, final_path: str) -> int:
        """流式下载视频（多段地址顺序追加，如 B站 html5 分段）"""
        part_path = final_path + ".part"
        size = 0
        try:
            with open(part_path, "wb") as f:
                for u in urls:
                    async with self._client().stream("GET", u, headers=self._headers_for(u)) as resp:
                        if resp.status_code != 200:
                            raise RuntimeError(f"下载HTTP {resp.status_code}: {u[:120]}")
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            f.write(chunk)
                            size += len(chunk)
            os.replace(part_path, final_path)
            return size
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    async def _download_images(self, urls: list, final_path: str) -> int:
        """图集逐图下载后打包 zip"""
        images = []
        try:
            for idx, u in enumerate(urls):
                async with self._client().stream("GET", u, headers=self._headers_for(u)) as resp:
                    if resp.status_code != 200:
                        logger.warning(f"图集第 {idx + 1} 张下载失败 HTTP {resp.status_code}，跳过")
                        continue
                    data = await resp.aread()
                    tail = urlparse(u).path.lower()
                    ext = ".jpg"
                    for candidate in (".png", ".webp", ".jpeg", ".gif"):
                        if tail.endswith(candidate):
                            ext = candidate
                    images.append((f"image_{idx + 1:03d}{ext}", data))
            if not images:
                raise RuntimeError("图集全部图片下载失败")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                for name, data in images:
                    zf.writestr(name, data)
            content = buf.getvalue()
            with open(final_path + ".part", "wb") as f:
                f.write(content)
            os.replace(final_path + ".part", final_path)
            return len(content)
        finally:
            if os.path.exists(final_path + ".part"):
                try:
                    os.remove(final_path + ".part")
                except OSError:
                    pass

    def _client(self):
        """下载专用 httpx 客户端（不携带平台 Cookie，避免 Cookie 泄漏到 CDN）"""
        client = getattr(self, "_dl_client", None)
        if client is None:
            import httpx
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=15),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
            self._dl_client = client
        return client

    @staticmethod
    def _headers_for(url: str) -> dict:
        host = urlparse(url).netloc
        for platform, headers in DOWNLOAD_HEADERS.items():
            ref = urlparse(headers["Referer"]).netloc
            if platform in host or ref.split(".")[0] in host:
                return headers
        return {"User-Agent": WORKS_UA}


# 全局作品监控实例
works_monitor = WorksMonitor()
