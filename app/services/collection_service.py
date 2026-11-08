"""合集 / 多P 批量下载服务

把「一个合集链接」展开成一组 Work 记录并入现有下载队列，从而**完全复用**
已有的下载链路（断点续传、去重、平台串行限速、磁盘水位、归档同步、NAS 同步），
不另建一套下载体系。

与作品订阅(works_monitor)的关系：
- 作品订阅是「按主播周期拉新作品」，合集下载是「按链接一次性导入该合集全部集数」
- 两者最终都落到 works 表，因此下载/归档/同步逻辑只有一份

合集去重键 collection_key：
    bili:season:{season_id}     真合集（跨多个 BV）
    bili:multipart:{bvid}       同一 BV 的多个分P
    bili:single:{bvid}          单视频（等价于普通作品）
同一合集重复导入是幂等的：已入库的集会被跳过，只补新增的集。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.services.platform import PlatformFactory
from app.services.platform.base import WorkInfo
from app.services.platform_manager import platform_manager
from app.services.works_monitor import works_monitor

logger = logging.getLogger(__name__)


@dataclass
class CollectionPreview:
    """解析预览（不落库）"""
    platform: str = ""
    kind: str = ""
    title: str = ""
    collection_key: str = ""
    total: int = 0
    items: list = field(default_factory=list)

    def to_dict(self):
        return {
            "platform": self.platform,
            "kind": self.kind,
            "kind_name": {
                "collection": "合集",
                "multipart": "多P",
                "single": "单视频",
            }.get(self.kind, self.kind),
            "title": self.title,
            "collection_key": self.collection_key,
            "total": self.total,
            "items": [
                {
                    "episode_index": it.episode_index,
                    "work_id": it.work_id,
                    "part_index": it.part_index,
                    "title": it.title,
                    "duration": round(it.duration or 0, 1),
                    "publish_ts": it.publish_ts,
                }
                for it in self.items
            ],
        }


class CollectionService:
    """合集解析与导入"""

    # 单次导入的集数上限：防止误贴一个超大合集把磁盘塞满
    # （B站合集上限实测 74+ 集，这里给一个更宽松但有限的护栏）。
    # 注意上限作用于"实际选中的集数"而非合集总集数——预览大合集再勾选
    # 部分集导入是合法用法，对 total 一刀切会让提示语里的建议自相矛盾。
    MAX_ITEMS = 200

    def __init__(self):
        # 导入串行化：preview→入库是跨会话的 check-then-act，并发导入（双击、
        # 两个重叠请求）会双双通过存在检查后在 commit 时撞唯一约束，
        # 整批回滚 500，破坏"已入库的集会被跳过"的幂等契约
        self._import_lock = asyncio.Lock()

    async def _adapter_for(self, platform_name: str):
        adapter = await platform_manager.get(platform_name)
        if adapter is None:
            raise RuntimeError(f"平台适配器不可用: {platform_name}")
        return adapter

    def detect_platform(self, url: str) -> str:
        """按 URL 识别平台（目前只有 B站 支持合集下载）"""
        name = PlatformFactory.detect_platform(url)
        if not name:
            raise RuntimeError("无法识别链接所属平台")
        return name

    async def preview(self, url: str) -> CollectionPreview:
        """解析链接 -> 清单（不落库、不下载）"""
        url = (url or "").strip()
        if not url:
            raise RuntimeError("链接不能为空")
        platform_name = self.detect_platform(url)
        adapter = await self._adapter_for(platform_name)

        parse = getattr(adapter, "parse_collection", None)
        if parse is None:
            raise RuntimeError(
                f"该平台暂不支持合集下载（当前仅 B站 支持）：{platform_name}"
            )

        info = await parse(url)
        # 上限不在预览阶段卡：预览的目的就是"先看清单再决定下载哪些"，
        # 大合集预览直接报错会让部分导入的合法路径走不通
        return CollectionPreview(
            platform=platform_name, kind=info.kind, title=info.title,
            collection_key=info.collection_key, total=info.total, items=info.items,
        )

    async def import_to_works(self, url: str, room_id: int = None,
                              indices: list = None) -> dict:
        """解析并入库到 works 表（status=pending，交给现有下载队列）

        :param room_id: 归属主播。为空时自动按合集标题创建一个「仅作品订阅」的主播，
                        保证 works.creator_id 外键与归档目录逻辑不变。
        :param indices: 只导入这些集号（1 起）；为空表示全部
        """
        async with self._import_lock:
            return await self._import_to_works_inner(url, room_id, indices)

    async def _import_to_works_inner(self, url: str, room_id: int = None,
                                     indices: list = None) -> dict:
        preview = await self.preview(url)
        items = preview.items
        if indices:
            wanted = set(int(i) for i in indices)
            items = [it for it in items if it.episode_index in wanted]
            if not items:
                raise RuntimeError(f"所选集号 {sorted(wanted)} 在清单中不存在")
        # 指定主播时先做归属校验（只读），再卡集数上限——否则平台选错了
        # 却收到"集数超限"这种误导性报错
        if room_id:
            await self._validate_room(room_id, preview.platform)
        if len(items) > self.MAX_ITEMS:
            raise RuntimeError(
                f"本次将导入 {len(items)} 集，超过单次导入上限 {self.MAX_ITEMS} 集。"
                f"请在清单里只勾选部分集导入，或分批导入"
            )

        room_id = await self._ensure_room(room_id, preview)

        added, skipped = await self._insert_items(room_id, items, preview)
        logger.info(
            f"合集导入完成 [{preview.title[:40]}] 新增 {added} 集，已存在跳过 {skipped} 集"
        )
        return {
            "room_id": room_id,
            "title": preview.title,
            "kind": preview.kind,
            "kind_name": preview.to_dict()["kind_name"],
            "collection_key": preview.collection_key,
            "total": preview.total,
            "imported": added,
            "skipped": skipped,
            "selected": len(items),
            "message": f"已导入 {added} 集（已存在跳过 {skipped} 集），正在加入下载队列",
        }

    @staticmethod
    def _raise_if_room_invalid(room, platform: str):
        """归属主播合法性：下载队列按 room.platform 选适配器、按 works_enabled
        过滤——跨平台导入会用错适配器永久失败，导入到未订阅主播的新作品会被
        队列直接标 failed。"""
        if room.platform != platform:
            raise RuntimeError(
                f"指定主播平台({room.platform})与合集平台({platform})不一致，"
                f"请选择同平台主播，或留空自动创建合集专属主播"
            )
        if not room.works_enabled:
            raise RuntimeError(
                "该主播未开启作品订阅。请先在主播管理开启后再导入"
                "（直接代开会顺带触发其主页作品全量回填，故不在导入时代开）"
            )

    async def _validate_room(self, room_id: int, platform: str):
        """只读校验指定主播（不产生创建副作用，供集数上限检查前调用）"""
        from app.models import Room
        async with async_session() as session:
            room = await session.get(Room, room_id)
        if room is None:
            raise RuntimeError(f"指定主播不存在: {room_id}")
        self._raise_if_room_invalid(room, platform)

    async def _ensure_room(self, room_id: int, preview: CollectionPreview) -> int:
        """确定归属主播；未指定时复用/创建一个合集专属主播

        B站合集的 UP 主不一定有主页订阅，而 works.creator_id 非空且归档目录
        以主播为单位。这里用「合集标题」建一个只有作品订阅的主播最稳妥：
        归档会落在 {主播}/{作品}/ 下，与现有布局完全兼容。
        """
        from app.models import Room
        from app.services import archive

        async with async_session() as session:
            if room_id:
                room = await session.get(Room, room_id)
                if room is None:
                    raise RuntimeError(f"指定主播不存在: {room_id}")
                self._raise_if_room_invalid(room, preview.platform)
                return room.id

            # 以合集标题作为主播名找已存在的（同平台同名视为同一个）
            name = (preview.title or "B站合集").strip()[:100]
            found = (await session.execute(
                select(Room).where(
                    Room.platform == preview.platform,
                    Room.streamer_name == name,
                )
            )).scalars().first()
            if found:
                return found.id

            room = Room(
                url="",                      # 不监控直播
                home_url="",
                platform=preview.platform,
                platform_user_id=None,
                quality="origin",
                enabled=True,
                works_enabled=True,          # 让下载队列认这些作品（见 process_download_queue 的过滤）
                streamer_name=name,
                remark=f"合集下载：{preview.collection_key}",
            )
            session.add(room)
            await session.commit()
            await session.refresh(room)
            await archive.sync_folder_name(session, room)
            logger.info(f"为合集创建作品主播: {name} (id={room.id})")
            return room.id

    async def _insert_items(self, room_id: int, items: list,
                            preview: CollectionPreview) -> tuple:
        """入库（幂等）。已存在的 (creator_id, platform_work_id) 跳过。

        下载队列按 platform_work_id 去重，所以多P 场景必须让每集有唯一 id：
        platform_work_id 用 {bvid}#p{part} 形式（单P 时仍用 bvid）。
        """
        from app.models import Work
        from datetime import datetime, timezone

        added = skipped = 0
        async with async_session() as session:
            for it in items:
                wid = self._work_id_for(it)
                exists = await session.execute(
                    select(Work.id).where(
                        Work.creator_id == room_id,
                        Work.platform_work_id == wid,
                    )
                )
                if exists.first():
                    skipped += 1
                    continue
                work = Work(
                    creator_id=room_id,
                    platform_work_id=wid,
                    work_type="video",
                    title=(it.title or "")[:500],
                    publish_time=(
                        datetime.fromtimestamp(it.publish_ts, tz=timezone.utc).replace(tzinfo=None)
                        if it.publish_ts else None
                    ),
                    duration=it.duration or 0,
                    download_urls=None,      # 直链带时效签名，入库时不缓存，下载时现解析
                    status="pending",
                    collection_key=preview.collection_key,
                    collection_title=(preview.title or "")[:500],
                    episode_index=it.episode_index,
                )
                # 存在检查到提交之间存在并发窗口（启动文件认领/订阅回填可能同时
                # 插入同一作品）：用 SAVEPOINT 逐条兜底，撞唯一约束只丢这一条、
                # 计入 skipped，而不是整批回滚 500
                try:
                    async with session.begin_nested():
                        session.add(work)
                    added += 1
                except IntegrityError:
                    skipped += 1
                    continue
            if added:
                await session.commit()
        return added, skipped

    @staticmethod
    def _work_id_for(item: WorkInfo) -> str:
        """合集内每集的唯一 platform_work_id

        多P 需要 #pN 后缀（同一 bvid 的不同分P 是不同作品）；
        真合集的每集 bvid 天然不同，直接用 bvid。
        """
        if item.part_index and item.part_index > 1:
            return f"{item.work_id}#p{item.part_index}"
        # 多P 的第一集也要带 #p1 以与 bvid 原义区分？不需要：第一集就是 bvid 本身，
        # 这样与「作品订阅」直接订阅该视频时去重口径一致
        return item.work_id

    async def list_collections(self) -> list:
        """已导入的合集汇总（含各状态计数），供前端展示进度"""
        from app.models import Work
        from sqlalchemy import func

        async with async_session() as session:
            rows = (await session.execute(
                select(
                    Work.collection_key,
                    Work.collection_title,
                    Work.creator_id,
                    func.count(Work.id),
                    func.sum(func.coalesce(Work.file_size, 0)),
                )
                .where(Work.collection_key.isnot(None))
                .group_by(Work.collection_key, Work.collection_title, Work.creator_id)
            )).all()
            status_rows = (await session.execute(
                select(Work.collection_key, Work.status, func.count(Work.id))
                .where(Work.collection_key.isnot(None))
                .group_by(Work.collection_key, Work.status)
            )).all()

        stats = {}
        for ckey, status, cnt in status_rows:
            stats.setdefault(ckey, {})[status] = cnt

        out = []
        for ckey, ctitle, creator_id, total, size in rows:
            s = stats.get(ckey, {})
            out.append({
                "collection_key": ckey,
                "collection_title": ctitle,
                "room_id": creator_id,
                "total": total,
                "completed": s.get("completed", 0),
                "pending": s.get("pending", 0),
                "downloading": s.get("downloading", 0),
                "failed": s.get("failed", 0),
                "size_mb": round((size or 0) / 1024 / 1024, 2),
            })
        out.sort(key=lambda x: (-x["total"], x["collection_title"] or ""))
        return out

    async def collection_items(self, collection_key: str) -> list:
        """某个合集下的全部集（含下载状态），按集号排序"""
        from app.models import Work

        async with async_session() as session:
            rows = (await session.execute(
                select(Work)
                .where(Work.collection_key == collection_key)
                .order_by(Work.episode_index, Work.id)
            )).scalars().all()
        return [
            {
                "id": w.id,
                "episode_index": w.episode_index,
                "platform_work_id": w.platform_work_id,
                "title": w.title,
                "duration": round(w.duration or 0, 1),
                "status": w.status,
                "file_path": w.file_path,
                "file_size_mb": round((w.file_size or 0) / 1024 / 1024, 2),
                "error_message": w.error_message,
            }
            for w in rows
        ]

    async def kick_download(self):
        """导入后立即踢一次下载队列（不必等下一个 10s 轮询）"""
        try:
            await works_monitor.process_download_queue()
        except Exception as e:
            logger.warning(f"合集导入后触发下载队列失败（不影响，后续轮询会拾取）: {e}")


collection_service = CollectionService()
