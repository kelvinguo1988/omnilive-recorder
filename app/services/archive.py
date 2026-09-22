"""归档目录布局（单一来源）：以主播为顶层，直播录像与主播作品各自归栏。

    {output_dir}/{主播}/直播间/{YYYY-MM-DD}/{文件}
    {output_dir}/{主播}/作品/{文件}

目录名固定记在 Room.folder_name 上，分配后不随主播改名漂移——否则磁盘上的历史
归档会被反复拆开，DB 里已存的相对路径也全部对不上。同名主播跨平台时后来者取
「主播名(平台)」；同平台同主播（多个直播间）共用一个目录。

NAS 同步的镜像布局（sync_service 的 {主播}/直播 + {主播}/作品）与此同构。
"""
import asyncio
import glob
import json
import logging
import os
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Room, Recording, Work
from app.services.platform.base import PLATFORM_CN
from app.services.recorder import recorder

logger = logging.getLogger(__name__)

LIVE_SUBDIR = "直播间"
WORKS_SUBDIR = "作品"

# 旧版布局：直播录像在 {平台}/{主播}/{日期}/，作品在 works/{平台}/{主播}/
LEGACY_WORKS_ROOT = "works"
_CN_PLATFORMS = set(PLATFORM_CN.values())


def display_name(room: Room) -> str:
    """归档用的主播显示名（与文件名模板里 {streamer} 的取值口径一致）"""
    return room.streamer_name or room.remark or room.room_id or room.platform_user_id or f"主播{room.id}"


def folder_of(room: Room) -> str:
    return room.folder_name or recorder._sanitize_filename(display_name(room))


def live_root(room: Room) -> str:
    """该主播的直播间录像目录（不含日期层）"""
    return os.path.join(settings.output_dir, folder_of(room), LIVE_SUBDIR)


def works_root(room: Room) -> str:
    """该主播的作品目录"""
    return os.path.join(settings.output_dir, folder_of(room), WORKS_SUBDIR)


async def folder_platform_map() -> dict:
    """{归档目录名: 平台中文}

    新布局路径里不再有平台层级，文件列表要按归档目录反查所属平台。
    """
    async with async_session() as session:
        rooms = (await session.execute(select(Room))).scalars().all()
    return {folder_of(r): PLATFORM_CN.get(r.platform, r.platform) for r in rooms}


async def _unique_folder(session, room: Room, want: str) -> str:
    """撞名处理：同平台同名视为同一主播（共用目录）；裸名被别的平台占用则加平台后缀"""
    rows = (await session.execute(select(Room.id, Room.platform, Room.folder_name))).all()
    others = [(plat, name) for rid, plat, name in rows if name and rid != room.id]
    if any(name == want and plat != room.platform for plat, name in others) \
            and not any(name == want and plat == room.platform for plat, name in others):
        return f"{want}({PLATFORM_CN.get(room.platform, room.platform)})"
    return want


async def _has_archived_files(session, room: Room) -> bool:
    """该主播名下是否已有落过盘的文件（有则目录名固定，不再跟随显示名变化）"""
    for model, column in ((Recording, Recording.room_id), (Work, Work.creator_id)):
        found = await session.scalar(
            select(model.id).where(column == room.id, model.file_path.isnot(None), model.file_path != "")
        )
        if found:
            return True
    return False


async def sync_folder_name(session, room: Room, display: str = None) -> str:
    """确定并写回归档目录名（幂等）。

    主播名下还没有文件时允许跟随最新显示名——刚添加主播往往只有房间号，开播时
    才从平台探测到真名；一旦落过盘就固定，否则用户改主播名会把历史归档拆成两个目录。
    """
    want = recorder._sanitize_filename(display or display_name(room)) or f"主播{room.id}"
    if room.folder_name == want:
        return want
    if room.folder_name and await _has_archived_files(session, room):
        return room.folder_name
    room.folder_name = await _unique_folder(session, room, want)
    await session.commit()
    return room.folder_name


async def ensure_folder_name(session, room: Room) -> str:
    """为尚未分配目录名的主播补一个（启动迁移用；已分配的不动）"""
    if room.folder_name:
        return room.folder_name
    room.folder_name = await _unique_folder(session, room, recorder._sanitize_filename(display_name(room)))
    return room.folder_name


# ---------------------------------------------------------------- 存量迁移

def _legacy_candidates(room: Room) -> list:
    """旧布局下该主播可能占用的目录名（直播用 主播名/房间号，作品用 主播名/平台UID）"""
    out = []
    for raw in (room.streamer_name, room.remark, room.room_id, room.platform_user_id):
        name = recorder._sanitize_filename(raw) if raw else ""
        if name and name not in out:
            out.append(name)
    return out


def _owner_map(rooms: list) -> dict:
    """{(平台中文, 旧目录名): 新归档目录名}，按主播 id 先后取首个"""
    mapping = {}
    for room in rooms:
        plat_cn = PLATFORM_CN.get(room.platform, room.platform)
        for cand in _legacy_candidates(room):
            mapping.setdefault((plat_cn, cand), folder_of(room))
    return mapping


def _new_rel(rel: str, owners: dict, folders_by_platform: dict) -> str:
    """旧布局相对路径 -> 新布局相对路径；不属于旧布局返回 None"""
    parts = rel.split(os.sep)
    # 主播恰好叫「抖音/B站/快手」时新布局路径也像旧布局；第二段是归档栏目即已迁移
    if parts[0] in _CN_PLATFORMS and len(parts) >= 2 and parts[1] in (LIVE_SUBDIR, WORKS_SUBDIR):
        return None
    if parts[0] == LEGACY_WORKS_ROOT and len(parts) >= 4:
        plat_cn, legacy_dir = parts[1], parts[2]
        tail = parts[3:]
    elif len(parts) >= 3 and parts[0] in _CN_PLATFORMS:
        plat_cn, legacy_dir = parts[0], parts[1]
        tail = parts[2:]
    else:
        return None

    folder = owners.get((plat_cn, legacy_dir))
    if not folder:
        # 无主目录（主播已删/改名久远）：沿用原目录名，被别的平台占用时加后缀
        holder = folders_by_platform.get(legacy_dir)
        folder = legacy_dir if holder in (None, plat_cn) else f"{legacy_dir}({plat_cn})"
    sub = WORKS_SUBDIR if parts[0] == LEGACY_WORKS_ROOT else LIVE_SUBDIR
    return os.path.join(folder, sub, *tail)


def _move_file(src_abs: str, dst_abs: str) -> bool:
    """单文件搬迁：目标已存在且同大小（上次中断的重复）则丢源文件，否则原子改名"""
    try:
        if os.path.exists(dst_abs):
            if os.path.getsize(dst_abs) == os.path.getsize(src_abs):
                os.remove(src_abs)
                return True
            return False
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
        os.replace(src_abs, dst_abs)
        return True
    except OSError as e:
        logger.error(f"归档搬迁失败 {src_abs} -> {dst_abs}: {e}")
        return False


def _prune_empty_legacy_dirs(base: str):
    """自底向上删除迁移后留空的旧目录（非空说明有文件搬失败，保留原位）"""
    roots = [os.path.join(base, LEGACY_WORKS_ROOT)] + [os.path.join(base, cn) for cn in _CN_PLATFORMS]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirnames or not filenames:
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass


def _migrate_on_disk(owners: dict, holders: dict) -> dict:
    base = os.path.abspath(settings.output_dir)
    moved = failed = 0

    for root in [os.path.join(base, LEGACY_WORKS_ROOT)] + [os.path.join(base, cn) for cn in _CN_PLATFORMS]:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for name in filenames:
                src_abs = os.path.join(dirpath, name)
                rel = os.path.relpath(src_abs, base)
                if name.startswith("."):
                    continue
                new_rel = _new_rel(rel, owners, holders)
                if not new_rel:
                    continue
                if _move_file(src_abs, os.path.join(base, new_rel)):
                    moved += 1
                else:
                    failed += 1
    _prune_empty_legacy_dirs(base)
    return {"moved": moved, "failed": failed}


async def migrate_legacy_layout() -> dict:
    """把旧布局（平台/主播/日期、works/平台/主播）的存量文件搬到主播归档目录。

    在监控启动前执行。搬迁与 DB 回写用同一个映射函数，并以"新路径上确有此文件"
    为条件才改库；任一文件搬不动就保留原位与原路径（列表解析仍兼容旧布局），
    所以本函数可反复执行且不会造成数据丢失。
    """
    stats = {"moved": 0, "failed": 0, "records": 0, "works": 0}
    try:
        async with async_session() as session:
            rooms = (await session.execute(select(Room).order_by(Room.id))).scalars().all()
            for room in rooms:
                await ensure_folder_name(session, room)
            await session.commit()
        room_list = list(rooms)
        owners = _owner_map(room_list)
        holders = {folder_of(r): PLATFORM_CN.get(r.platform, r.platform) for r in room_list}
        base = os.path.abspath(settings.output_dir)

        if os.path.isdir(settings.output_dir):
            stats.update(await asyncio.to_thread(_migrate_on_disk, owners, holders))

        # DB 路径回写：只在新位置确实有文件时才改，避免指向不存在的文件

        def _rewrite(rel):
            if not rel:
                return None
            new_rel = _new_rel(rel, owners, holders)
            if not new_rel:
                return None
            target = os.path.join(base, new_rel)
            if "%04d" in new_rel:
                # 分段 part 存的是 ffmpeg 模板路径（含 %04d），实际文件按通配核对
                return new_rel if glob.glob(target.replace("%04d", "*")) else None
            return new_rel if os.path.isfile(target) else None

        async with async_session() as session:
            for row in (await session.execute(select(Recording))).scalars().all():
                new_rel = _rewrite(row.file_path)
                if new_rel:
                    row.file_path = new_rel
                    row.file_name = os.path.basename(new_rel)
                    stats["records"] += 1
                if row.part_paths:
                    try:
                        parts = json.loads(row.part_paths)
                    except (ValueError, TypeError):
                        continue
                    new_parts = [_rewrite(p) or p for p in parts]
                    if new_parts != parts:
                        row.part_paths = json.dumps(new_parts)
            for row in (await session.execute(select(Work))).scalars().all():
                new_rel = _rewrite(row.file_path)
                if new_rel:
                    row.file_path = new_rel
                    stats["works"] += 1
            await session.commit()

        if stats["moved"] or stats["records"] or stats["works"]:
            logger.info(
                f"归档布局迁移完成：搬迁 {stats['moved']} 个文件，"
                f"更新录制记录 {stats['records']} 条、作品 {stats['works']} 条"
                + (f"，{stats['failed']} 个文件保留在原位置" if stats["failed"] else "")
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"归档布局迁移异常: {e}")
    return stats
