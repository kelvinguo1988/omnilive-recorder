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
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select, update, or_

from app.database import async_session
from app.models import Room, Work
from app.config import settings
from app.utils import utcnow
from app.services.platform.base import WorkInfo, WORKS_UA, PLATFORM_CN
from app.services import archive
from app.services.platform_manager import platform_manager
from app.services.file_manager import file_manager
from app.services.recorder import recorder

logger = logging.getLogger(__name__)

# 各平台下载 CDN 的请求头：B站/快手 CDN 校验 Referer，抖音 CDN 校验 UA 一致性
DOWNLOAD_HEADERS = {
    "douyin": {"User-Agent": WORKS_UA, "Referer": "https://www.douyin.com/"},
    "bilibili": {"User-Agent": WORKS_UA, "Referer": "https://www.bilibili.com/"},
    "kuaishou": {"User-Agent": WORKS_UA, "Referer": "https://www.kuaishou.com/"},
}

# P1-5: 各平台 CDN 的显式域名白名单（按 host 后缀匹配，见 _headers_for）。
# 快手作品 CDN 实际域名是 *.yximgs.com / *.kwaicdn.com，原"platform in host"猜法
# 全部落空 → 无 Referer → 403。
DOWNLOAD_HOSTS = {
    "douyin": ("douyin.com", "douyinpic.com", "douyinvod.com", "bytecdn.cn",
               "bytedance.com", "zjcdn.com", "ixigua.com", "pstatp.com",
               "snssdk.com", "amemv.com"),
    "bilibili": ("bilibili.com", "hdslb.com", "bilivideo.com", "acgvideo.com",
                 "biliapi.net", "akamaized.net"),
    "kuaishou": ("kuaishou.com", "yximgs.com", "kwimgs.com", "kwaicdn.com",
                 "kwai.com", "gifshow.com", "kuaishouzt.com", "chenzhongtech.com",
                 "kscdn.com", "kspkg.com"),
}

def _redact_url(url: str, keep: int = 40) -> str:
    """去掉 URL 的 query（通常含签名 token）与超长部分，仅用于日志/错误信息。

    P1-9: CDN 直链带 txTime/txSecret/a_bogus 等时效凭据，不能进 error_message
    再被 API 回显给前端。这里只保留 scheme+host+path 前缀。
    """
    try:
        p = urlparse(url)
        host = p.netloc or ""
        path = (p.path or "")[:keep]
        return f"{host}{path}" + ("…" if len(p.path or "") > keep else "")
    except Exception:
        return (url or "")[:keep]


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
        # P0-10: 重入保护必须是 **per-room**，不能是全局单锁。
        # 原实现用一把全局 _check_lock 包住整个 _check_room_works_inner，
        # 而其中 _backfill() 的整条翻页链（每页 asyncio.sleep(2~5)、最多 500 页、
        # works_backfill_limit 默认 0=不限量）都在锁内 —— 于是一个历史作品多的主播
        # 回填期间，其余所有主播的增量检查、以及 POST /api/works/check/{id}
        # 触发的立即检查全部排队数十分钟到数小时，用户点"立即检查"看起来无响应。
        self._check_locks: dict[int, asyncio.Lock] = {}
        # 磁盘水位告警节流
        self._last_disk_warn: float = 0.0

    def _check_lock_for(self, room_id: int) -> asyncio.Lock:
        """取该主播的检查锁（惰性创建），只串行化同一主播的并发检查"""
        lock = self._check_locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            self._check_locks[room_id] = lock
        return lock

    # ---------- 生命周期 ----------

    async def start(self):
        if self._running:
            return
        self._running = True
        await self._recover_stale_works()
        await self.import_existing_files()
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

    # ---------- 磁盘已有文件认领（重建/迁移后防重复下载） ----------

    # 作品文件扩展名（视频/图集zip），与 _download_work 的命名约定对应
    _WORK_EXTS = (".mp4", ".zip", ".flv", ".ts", ".mkv")

    def _works_file_index(self, room: Room) -> dict:
        """扫描主播作品目录，返回 {platform_work_id: 相对路径}。

        文件名约定（_download_work 生成）：{YYYYMMDD}_{标题}_{作品ID}.{扩展名}，
        作品ID 取文件名主干末段（rsplit 防标题含下划线）。
        """
        dir_path = archive.works_root(room)
        index = {}
        if not os.path.isdir(dir_path):
            return index
        for name in os.listdir(dir_path):
            stem, ext = os.path.splitext(name)
            if ext.lower() not in self._WORK_EXTS or name.startswith("."):
                continue
            wid = stem.rsplit("_", 1)[-1]
            if wid:
                index[wid] = os.path.relpath(os.path.join(dir_path, name),
                                             settings.output_dir)
        return index

    def _cleanup_part_files(self, room: Room):
        """删除主播作品目录下的 .part 半成品（下载中断残留，无完整数据）"""
        dir_path = archive.works_root(room)
        if not os.path.isdir(dir_path):
            return
        for name in os.listdir(dir_path):
            if name.endswith(".part"):
                try:
                    os.remove(os.path.join(dir_path, name))
                except OSError:
                    pass

    async def import_existing_files(self):
        """启动时认领磁盘上已存在的作品文件（对标直播录制的启动自愈）。

        解决重建容器/迁移/换库后的重复下载：
        - 磁盘有文件但 DB 无记录（如数据库重置）→ 以 completed 导入，标题从文件名还原
        - DB 有记录但状态非完成且磁盘文件存在（如状态残留）→ 修复为 completed
        全部走唯一约束与 ID 索引比对，不发起任何网络请求。
        """
        try:
            async with async_session() as session:
                rooms = (await session.execute(
                    select(Room).where(Room.works_enabled == True)
                )).scalars().all()

            imported = fixed = 0
            for room in rooms:
                # 清理上次异常退出残留的 .part 半成品（不被索引认领, 只会越积越多）
                await asyncio.to_thread(self._cleanup_part_files, room)
                index = await asyncio.to_thread(self._works_file_index, room)
                if not index:
                    continue
                async with async_session() as session:
                    rows = (await session.execute(
                        select(Work).where(Work.creator_id == room.id)
                    )).scalars().all()
                    by_id = {w.platform_work_id: w for w in rows}
                    for wid, rel in index.items():
                        abs_path = os.path.join(settings.output_dir, rel)
                        size = os.path.getsize(abs_path)
                        w = by_id.get(wid)
                        if w is None:
                            stem = os.path.splitext(os.path.basename(rel))[0]
                            date_part, _, rest = stem.partition("_")
                            title = rest.rsplit("_", 1)[0] if "_" in rest else rest
                            session.add(Work(
                                creator_id=room.id,
                                platform_work_id=wid,
                                work_type="images" if rel.lower().endswith(".zip") else "video",
                                title=title[:500] or None,
                                file_path=rel,
                                file_size=size,
                                status="completed",
                                downloaded_at=utcnow(),
                            ))
                            imported += 1
                        elif w.status != "completed":
                            w.file_path = rel
                            w.file_size = size
                            w.status = "completed"
                            w.error_message = None
                            fixed += 1
                    await session.commit()
            if imported or fixed:
                logger.warning(
                    f"作品文件认领完成: 新导入 {imported} 条, 修复状态 {fixed} 条"
                    f"（磁盘已有文件不再重新下载）"
                )
        except Exception as e:
            logger.error(f"作品文件认领失败: {e}")

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
        # P0-10: 只锁该主播自己，不同主播可并行检查（互不阻塞）
        async with self._check_lock_for(room.id):
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

        now = utcnow()
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
            added = await self._insert_new_works(room.id, works)
            total += added
            page_guard += 1
            if limit and total >= limit:
                logger.info(f"主播 {label} 回填达到上限 {limit} 条，截断")
                break
            if truncated or not has_more or not works or page_guard >= 500:
                # 游客态/风控常见截断特征：首页满页 has_more=True 但次页即空。
                # 明确提示，避免用户误以为已回填全部
                if total >= settings.works_check_count and not works:
                    logger.warning(
                        f"主播 {label} 翻页提前终止（累计 {total} 条后次页为空）："
                        f"游客态/风控可能截断了分页，配置该平台登录态 Cookie 后"
                        f"点「重新回填」可补全历史作品"
                    )
                break
            # 原地踏步保护：游标不前进或本页无新增却仍有 has_more，终止防死循环
            if next_cursor == cursor or (not added and works):
                logger.warning(f"主播 {label} 翻页游标未前进/无新增，提前终止（防循环）")
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
        """作品入库（查重+唯一约束兜底），返回新增条数。

        入库前比对磁盘：该作品的文件已存在（重建/迁移后重新回填的场景）
        → 直接以 completed 入库，不再进下载队列；只对磁盘没有的作品排队下载。
        """
        added = 0
        async with async_session() as session:
            room = await session.get(Room, room_id)
            disk_index = await asyncio.to_thread(self._works_file_index, room) if room else {}
            for w in works:
                exists = await session.execute(
                    select(Work.id).where(
                        Work.creator_id == room_id,
                        Work.platform_work_id == w.work_id,
                    )
                )
                if exists.first():
                    continue
                rel_path = disk_index.get(w.work_id)
                if rel_path:
                    status = "completed"
                    file_size = os.path.getsize(os.path.join(settings.output_dir, rel_path))
                    file_path = rel_path
                else:
                    status = "pending"
                    file_size = 0
                    file_path = None
                session.add(Work(
                    creator_id=room_id,
                    platform_work_id=w.work_id,
                    work_type=w.work_type,
                    title=(w.title or "")[:500],
                    publish_time=datetime.fromtimestamp(w.publish_ts, tz=timezone.utc).replace(tzinfo=None) if w.publish_ts else None,
                    duration=w.duration,
                    download_urls=json.dumps(w.download_urls, ensure_ascii=False) if w.download_urls else None,
                    status=status,
                    file_path=file_path,
                    file_size=file_size,
                    downloaded_at=utcnow() if status == "completed" else None,
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
        if file_manager.disk_over_limit():
            if time.time() - self._last_disk_warn > 600:
                self._last_disk_warn = time.time()
                logger.warning(
                    f"磁盘使用率已达上限 {settings.max_disk_usage}%，暂停作品下载；"
                    f"清理 {settings.output_dir} 或在设置页调高水位后自动恢复"
                )
            return
        async with async_session() as session:
            # P1-2: 只取「所属主播仍启用且仍订阅」的作品。
            # 原实现取最前 100 条 pending，命中禁用/已删主播时只 `continue` 不更新状态，
            # 于是前 100 条若全属被禁用主播，它们永远 pending、永远占住窗口，
            # 后面正常主播的新作品**永久饥饿**（表现为"新作品一直待下载"）。
            # 这里在查询层就过滤掉，并顺手把它们标记为 skipped，清出队列窗口。
            result = await session.execute(
                select(Work)
                .join(Room, Work.creator_id == Room.id)
                .where(
                    Work.status == "pending",
                    Room.enabled == True,
                    Room.works_enabled == True,
                )
                .order_by(Work.id)
                .limit(100)
            )
            pendings = result.scalars().all()

            # 把"主播已禁用/已删/已取消订阅"的 pending 作品置为 failed，
            # 避免它们无限占用队列表头（每次轮询都重新排在最前）
            stale = (await session.execute(
                select(Work.id)
                .outerjoin(Room, Work.creator_id == Room.id)
                .where(
                    Work.status == "pending",
                    or_(
                        Room.id.is_(None),
                        Room.enabled == False,
                        Room.works_enabled == False,
                    ),
                )
                .limit(200)
            )).scalars().all()
            if stale:
                await session.execute(
                    update(Work).where(Work.id.in_(stale)).values(
                        status="failed",
                        error_message="主播已禁用/已删除/已取消作品订阅，跳过下载",
                    )
                )
                await session.commit()
                logger.info(f"跳过 {len(stale)} 条属于已禁用主播的待下载作品，释放队列窗口")
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
            # P1-3: 平台名必须先白名单校验再取信号量。
            # 原实现 `self._download_sems[platform]` 直接索引，字典只由 PLATFORM_CN
            # （douyin/bilibili/kuaishou）初始化；库里若出现脏 platform（如 "Douyin"、
            # 空串、历史遗留值）就抛 KeyError，被 _download_loop 的 except 每 10s 吞一次
            # → **该平台所有下载静默停摆**，日志里只有一行"作品下载队列异常"。
            if platform not in PLATFORM_CN:
                logger.error(
                    f"作品下载跳过：主播 {room.id} 平台值非法 {platform!r}"
                    f"（应为 {list(PLATFORM_CN)}），请修正该主播记录"
                )
                async with async_session() as session:
                    await session.execute(
                        update(Work).where(Work.id == work.id).values(
                            status="failed",
                            error_message=f"主播平台值非法: {platform!r}",
                        )
                    )
                    await session.commit()
                continue
            # setdefault 兜底：即使上面校验通过，也避免未来新增平台时再次 KeyError
            sem = self._download_sems.setdefault(platform, asyncio.Semaphore(1))
            async with sem:
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
            row = await session.get(Room, room.id)
            if row:
                await archive.sync_folder_name(session, row)
                room.folder_name = row.folder_name
            await session.commit()

        # 解析直链：优先列表时缓存的地址，否则二次解析（B站）
        #
        # P1-4: 抖音/快手的 CDN 直链是带时效签名的，DB 里缓存的 download_urls 过期后
        # 下载必然 403/失败。原实现只用缓存、从不重解析，于是作品**永久 failed**，
        # 连 /retry 端点也走同一份过期 URL、永远失败。
        # 现在：解析出可用地址 → 下载；若下载失败，清空缓存并重解析一次再试。
        urls = self._cached_urls(work)
        if not urls:
            urls = await self._resolve_urls(adapter, work)
        if not urls:
            raise RuntimeError("未能获取下载地址（可能被风控或作品已删除）")

        dir_path = archive.works_root(room)
        os.makedirs(dir_path, exist_ok=True)

        date_str = work.publish_time.strftime("%Y%m%d") if work.publish_time else \
            utcnow().strftime("%Y%m%d")
        title = recorder._sanitize_filename(work.title or "")[:60] or "untitled"
        base = f"{date_str}_{title}_{work.platform_work_id}"

        # P1-4: 首次尝试失败（多为缓存直链过期 403）时，清空缓存并重解析一次再试。
        # 抖音/快手未覆写 get_download_urls，缓存过期后旧实现永久失败、/retry 也救不回来。
        last_error = None
        for attempt in (1, 2):
            if work.work_type == "images":
                final_path = os.path.join(dir_path, f"{base}.zip")
                try:
                    size = await self._download_images(urls, final_path)
                    break
                except Exception as e:
                    last_error = e
            else:
                ext = ".mp4"
                for u in urls[:1]:
                    tail = urlparse(u).path.lower()
                    for candidate in (".flv", ".ts", ".mkv"):
                        if tail.endswith(candidate):
                            ext = candidate
                final_path = os.path.join(dir_path, f"{base}{ext}")
                try:
                    size = await self._download_video(urls, final_path)
                    break
                except Exception as e:
                    last_error = e
            if attempt == 1:
                # 清空过期缓存，重解析后重试
                logger.warning(
                    f"作品 {work.platform_work_id} 首次下载失败，清空过期直链缓存后重试: {last_error}"
                )
                await self._clear_cached_urls(work)
                urls = await self._resolve_urls(adapter, work)
                if not urls:
                    raise RuntimeError(f"重新解析下载地址失败: {last_error}")
        else:
            raise RuntimeError(str(last_error) if last_error else "下载失败")

        async with async_session() as session:
            await session.execute(
                update(Work).where(Work.id == work.id).values(
                    status="completed",
                    file_path=os.path.relpath(final_path, settings.output_dir),
                    file_size=size,
                    error_message=None,
                    downloaded_at=utcnow(),
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
                            # P1-9: 不能把带签名 token 的 CDN 直链写进错误信息 ——
                            # 它会被存进 Work.error_message 并由 /api/works 原样回显给前端，
                            # 等于把时效性凭据暴露给任何能打开页面的人。只保留 host+路径前缀。
                            safe = _redact_url(u)
                            raise RuntimeError(f"下载HTTP {resp.status_code}: {safe}")
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
    def _cached_urls(work: Work) -> list:
        """从 DB 缓存的 download_urls 解析直链列表（损坏 JSON 视为空）"""
        if not work.download_urls:
            return []
        try:
            urls = json.loads(work.download_urls)
        except (ValueError, TypeError):
            return []
        return urls if isinstance(urls, list) else []

    @staticmethod
    async def _resolve_urls(adapter, work: Work) -> list:
        """二次解析下载地址（B站等需额外 API 调用；抖音/快手回退到列表缓存）"""
        info = WorkInfo(work_id=work.platform_work_id, work_type=work.work_type,
                        title=work.title or "")
        try:
            return await adapter.get_download_urls(info) or []
        except Exception as e:
            logger.warning(f"二次解析下载地址失败 {work.platform_work_id}: {e}")
            return []

    @staticmethod
    async def _clear_cached_urls(work: Work):
        """清空过期直链缓存，使后续重解析生效"""
        async with async_session() as session:
            await session.execute(
                update(Work).where(Work.id == work.id).values(download_urls=None)
            )
            await session.commit()
        work.download_urls = None

    @staticmethod
    def _headers_for(url: str) -> dict:
        """按目标 URL 的 host 选择该平台 CDN 需要的请求头。

        P1-5: 原实现用 `platform in host or Referer首段 in host` 猜平台。
        快手作品 CDN 的实际域名是 *.yximgs.com / *.kwaicdn.com —— 两个条件都不命中，
        于是落到"只带 UA、无 Referer"，而快手 CDN 校验 Referer → 下载 403，
        作品永久 failed。这里改为显式 host 后缀白名单（DOWNLOAD_HOSTS）。
        """
        host = (urlparse(url).netloc or "").lower()
        for platform, domains in DOWNLOAD_HOSTS.items():
            if any(host == d or host.endswith("." + d) for d in domains):
                return DOWNLOAD_HEADERS[platform]
        return {"User-Agent": WORKS_UA}


# 全局作品监控实例
works_monitor = WorksMonitor()
