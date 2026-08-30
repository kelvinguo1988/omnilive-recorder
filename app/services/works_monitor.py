"""作品订阅监控 - 定时检测创作者新作品并自动下载

独立于直播监控的 asyncio 循环：
- 每个 works_poll_interval 检查所有启用创作者的最新作品列表
- 首次添加的创作者全量回填历史作品（works_backfill_limit 可限制条数）
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
from app.models import Creator, Work
from app.config import settings
from app.services.platform import PlatformFactory
from app.services.platform.base import WorkInfo, WORKS_UA
from app.services.recorder import recorder

logger = logging.getLogger(__name__)

PLATFORM_CN = {"douyin": "抖音", "bilibili": "B站", "kuaishou": "快手"}

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
        self._platform_instances: dict = {}
        # 每平台下载并发=1，避免同一平台并发请求触发风控
        self._download_sems: dict = {p: asyncio.Semaphore(1) for p in PLATFORM_CN}
        self._last_download_ts: dict = {}
        # 重入保护：API 触发的立即检查与循环检查不并发执行
        self._check_lock = asyncio.Lock()

    # ---------- 平台适配器管理（同直播监控，Cookie 变化自动重建） ----------

    async def _get_platform(self, platform_name: str):
        cookie = ""
        if platform_name == "douyin":
            cookie = settings.douyin_cookie
        elif platform_name == "bilibili":
            cookie = settings.bilibili_cookie
        elif platform_name == "kuaishou":
            cookie = settings.kuaishou_cookie
        proxy = settings.proxy_addr if settings.enable_proxy else ""

        cached = self._platform_instances.get(platform_name)
        if cached is not None and getattr(cached, "cookie", None) == cookie \
                and getattr(cached, "proxy", None) == proxy:
            return cached
        if cached is not None:
            try:
                await cached.close()
            except Exception:
                pass

        instance = PlatformFactory.get_platform(
            platform_name, proxy=proxy, cookie=cookie, timeout=settings.check_timeout,
        )
        if instance:
            self._platform_instances[platform_name] = instance
        return self._platform_instances.get(platform_name)

    async def _reset_platform_cache(self):
        for inst in list(self._platform_instances.values()):
            try:
                await inst.close()
            except Exception:
                pass
        self._platform_instances.clear()

    # ---------- 生命周期 ----------

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("作品订阅监控已启动")

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
        await self._reset_platform_cache()
        logger.info("作品订阅监控已停止")

    async def _loop(self):
        # 启动后稍等，避免与直播监控/应用启动争抢
        await asyncio.sleep(5)
        while self._running:
            try:
                await self.check_all_creators()
            except Exception as e:
                logger.error(f"作品检查循环异常: {e}")
            try:
                await self.process_download_queue()
            except Exception as e:
                logger.error(f"作品下载队列异常: {e}")
            interval = settings.works_poll_interval
            # 分段 sleep，保证 stop() 能及时退出
            waited = 0.0
            while self._running and waited < interval:
                step = min(5.0, interval - waited)
                await asyncio.sleep(step)
                waited += step

    # ---------- 检查 ----------

    async def check_all_creators(self):
        async with async_session() as session:
            result = await session.execute(select(Creator).where(Creator.enabled == True))
            creators = result.scalars().all()
        for creator in creators:
            if not self._running:
                break
            try:
                await self.check_creator(creator)
            except Exception as e:
                logger.error(f"检查创作者 {creator.nickname or creator.id} 失败: {e}")
            await asyncio.sleep(random.uniform(*CHECK_JITTER))

    async def check_creator_by_id(self, creator_id: int):
        """API 触发的立即检查（按 id 重读创作者）"""
        async with async_session() as session:
            result = await session.execute(select(Creator).where(Creator.id == creator_id))
            creator = result.scalar_one_or_none()
        if creator:
            await self.check_creator(creator)

    async def check_creator(self, creator: Creator):
        """检查单个创作者：未回填则全量回填，否则增量拉最新一页"""
        async with self._check_lock:
            await self._check_creator_inner(creator)

    async def _check_creator_inner(self, creator: Creator):
        adapter = await self._get_platform(creator.platform)
        if not adapter:
            logger.warning(f"作品订阅不支持的平台: {creator.platform}")
            return

        # 昵称为空时补一次用户信息（添加时网络失败的可能已恢复）
        if not creator.nickname:
            info = await adapter.get_user_info(creator.platform_user_id)
            if info.nickname:
                async with async_session() as session:
                    await session.execute(
                        update(Creator).where(Creator.id == creator.id).values(
                            nickname=info.nickname, avatar_url=info.avatar_url or None,
                        )
                    )
                    await session.commit()
                creator.nickname = info.nickname

        added = 0
        try:
            if not creator.backfill_done:
                added = await self._backfill(creator, adapter)
            else:
                works, _, _ = await adapter.get_user_works(
                    creator.platform_user_id, 0, settings.works_check_count
                )
                added = await self._insert_new_works(creator.id, works)
        except Exception as e:
            # 拉取失败（风控/网络/Cookie）：保留 backfill_done=False，下个周期自动重试
            logger.warning(
                f"作品拉取失败 [{PLATFORM_CN.get(creator.platform, creator.platform)}] "
                f"{creator.nickname or creator.platform_user_id}: {e}"
            )

        now = datetime.utcnow()
        async with async_session() as session:
            await session.execute(
                update(Creator).where(Creator.id == creator.id).values(last_check_time=now)
            )
            await session.commit()

        label = creator.nickname or creator.platform_user_id
        logger.info(f"作品检查完成 [{PLATFORM_CN.get(creator.platform, creator.platform)}] {label}: 新增 {added} 条")

    async def _backfill(self, creator: Creator, adapter) -> int:
        """全量回填历史作品（翻页直到没有更多），works_backfill_limit 限制条数"""
        limit = settings.works_backfill_limit
        cursor = 0
        total = 0
        page_guard = 0
        truncated = False
        label = creator.nickname or creator.platform_user_id
        while True:
            works, next_cursor, has_more = await adapter.get_user_works(
                creator.platform_user_id, cursor, settings.works_check_count
            )
            # 上限截断：只入库剩余配额内的作品
            if limit and total + len(works) > limit:
                works = works[: max(limit - total, 0)]
                truncated = True
            total += await self._insert_new_works(creator.id, works)
            page_guard += 1
            if limit and total >= limit:
                logger.info(f"创作者 {label} 回填达到上限 {limit} 条，截断")
                break
            if truncated or not has_more or not works or page_guard >= 500:
                break
            cursor = next_cursor
            await asyncio.sleep(random.uniform(2.0, 5.0))

        async with async_session() as session:
            await session.execute(
                update(Creator).where(Creator.id == creator.id).values(backfill_done=True)
            )
            await session.commit()
        logger.info(f"创作者 {label} 历史作品回填完成，共 {total} 条")
        return total

    async def _insert_new_works(self, creator_id: int, works: list) -> int:
        """作品入库（查重+唯一约束兜底），返回新增条数"""
        added = 0
        async with async_session() as session:
            for w in works:
                exists = await session.execute(
                    select(Work.id).where(
                        Work.creator_id == creator_id,
                        Work.platform_work_id == w.work_id,
                    )
                )
                if exists.first():
                    continue
                session.add(Work(
                    creator_id=creator_id,
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

        creator_cache: dict = {}
        for work in pendings:
            if not self._running:
                break
            if work.creator_id not in creator_cache:
                async with async_session() as session:
                    res = await session.execute(
                        select(Creator).where(Creator.id == work.creator_id)
                    )
                    creator_cache[work.creator_id] = res.scalar_one_or_none()
            creator = creator_cache[work.creator_id]
            if not creator or not creator.enabled:
                continue

            platform = creator.platform
            async with self._download_sems[platform]:
                # 平台级随机限速
                gap = random.uniform(*DOWNLOAD_GAP)
                wait = self._last_download_ts.get(platform, 0) + gap - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    await self._download_work(creator, work)
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

    async def _download_work(self, creator: Creator, work: Work):
        """下载单个作品（视频→单文件；图集→打包 zip），.part 临时文件原子改名"""
        adapter = await self._get_platform(creator.platform)
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
            PLATFORM_CN.get(creator.platform, creator.platform),
            recorder._sanitize_filename(creator.nickname or creator.platform_user_id),
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

        label = creator.nickname or creator.platform_user_id
        logger.info(
            f"作品下载完成 [{PLATFORM_CN.get(creator.platform, creator.platform)}] "
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
