"""归档布局回归自检：存量迁移 + 新布局写入 + 文件扫描归类（不触网，全部在 /tmp 沙箱里跑）

用途：改动 app/services/archive.py / recorder.py / monitor.py / works_monitor.py /
file_manager.py 的归档路径口径后，运行本脚本确认：
  1. 旧布局（平台/主播/日期、works/平台/主播）存量文件被逐个搬进 主播/{直播间,作品}
  2. 搬迁失败/无主目录不丢文件，重复启动幂等
  3. DB 里的相对路径与 part 模板同步回写
  4. 新录制、新作品下载写入新归档目录；主播改名不漂移目录
  5. 文件列表/主播汇总按新布局归类，并按 folder_name 补出平台

用法：python scripts/check_archive_layout.py
"""
import asyncio
import json
import os
import shutil
import sys

BASE = "/tmp/lr_archive_check"
OUT = os.path.join(BASE, "out")

# 必须在导入 app.* 之前设置：配置从环境变量读取
os.environ["LIVE_RECORDER_OUTPUT_DIR"] = OUT
os.environ["LIVE_RECORDER_DATABASE_URL"] = f"sqlite+aiosqlite:///{BASE}/data/rec.db"
os.environ["LIVE_RECORDER_SYNC_ENABLED"] = "false"
os.environ["CONFIG_PATH"] = os.path.join(BASE, "nonexistent.ini")

shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import async_session, init_db  # noqa: E402
from app.models import Recording, Room, Work  # noqa: E402
from app.services import archive  # noqa: E402
from app.services.file_manager import file_manager  # noqa: E402
from app.services.recorder import recorder  # noqa: E402

assert settings.output_dir == OUT, settings.output_dir

FAILED = []


def touch(rel, size=100):
    path = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


def check(cond, msg):
    if cond:
        print(f"  ok  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILED.append(msg)


async def main():
    await init_db()

    # ---- 旧布局文件 + 对应 DB 记录（路径全是旧口径）
    legacy_live = "抖音/小美/2026-09-20/小美_200101.ts"
    legacy_by_roomid = "抖音/123/2026-09-21/小美_210101.ts"
    legacy_bili = "B站/阿B/2026-09-20/阿B_200101.ts"
    legacy_ks = "快手/小美/2026-09-20/小美_200101.ts"
    legacy_orphan = "抖音/已注销/2026-09-19/x.ts"
    legacy_work = "works/抖音/小美/20260920_标题_7001.mp4"
    legacy_work_orphan = "works/快手/ghost/20260920_标题_7002.mp4"
    # 旧目录名撞上别的平台的归档目录（阿B 属于 B站）：应加平台后缀避免混档
    legacy_clash = "抖音/阿B/2026-09-22/clash.ts"
    # 上一轮中断残留的同大小重复文件：迁移应去重而不是报错
    legacy_dup = "B站/阿B/2026-09-20/dup.ts"
    # 进行中场次的 part 模板（真实分片文件在磁盘上）
    legacy_part = "抖音/小美/2026-09-20/小美_200101_part1_0001.ts"
    for rel in (legacy_live, legacy_by_roomid, legacy_bili, legacy_ks, legacy_orphan,
                legacy_work, legacy_work_orphan, legacy_clash, legacy_part):
        touch(rel)
    touch(legacy_dup, size=222)
    touch("阿B/直播间/2026-09-20/dup.ts", size=222)

    async with async_session() as s:
        rooms = [
            Room(url="https://live.douyin.com/123", platform="douyin", room_id="123",
                 streamer_name="小美", works_enabled=True, platform_user_id="MS4w"),
            Room(url="https://live.bilibili.com/999", platform="bilibili", room_id="999",
                 streamer_name="阿B"),
            Room(url="https://live.douyin.com/456", platform="douyin", room_id="456",
                 streamer_name="小美"),
            Room(url="https://live.kuaishou.com/u/xiaomei", platform="kuaishou",
                 room_id="xiaomei", streamer_name="小美"),
        ]
        for r in rooms:
            s.add(r)
        await s.commit()
        for r in rooms:
            await s.refresh(r)
        r1, r2, r3, r4 = rooms

        s.add(Recording(room_id=r1.id, file_path=legacy_live,
                        file_name=os.path.basename(legacy_live), format="ts",
                        status="completed",
                        part_paths=json.dumps([legacy_part.replace("_0001.", "_%04d.")])))
        s.add(Recording(room_id=r3.id, file_path=legacy_by_roomid,
                        file_name=os.path.basename(legacy_by_roomid), format="ts",
                        status="completed"))
        s.add(Recording(room_id=r2.id, file_path=legacy_bili,
                        file_name=os.path.basename(legacy_bili), format="ts",
                        status="completed"))
        s.add(Recording(room_id=r4.id, file_path=legacy_ks,
                        file_name=os.path.basename(legacy_ks), format="ts",
                        status="completed"))
        s.add(Work(creator_id=r1.id, platform_work_id="7001", work_type="video",
                   file_path=legacy_work, status="completed"))
        await s.commit()

    print("== 存量迁移 ==")
    stats = await archive.migrate_legacy_layout()
    print(f"  stats={stats}")
    check(stats["failed"] == 0, "无搬迁失败")
    check(stats["moved"] == 10, f"10 个存量文件全部搬迁（实际 {stats['moved']}）")

    expect = {
        legacy_live: "小美/直播间/2026-09-20/小美_200101.ts",
        legacy_by_roomid: "小美/直播间/2026-09-21/小美_210101.ts",
        legacy_bili: "阿B/直播间/2026-09-20/阿B_200101.ts",
        legacy_ks: "小美(快手)/直播间/2026-09-20/小美_200101.ts",
        legacy_orphan: "已注销/直播间/2026-09-19/x.ts",
        legacy_work: "小美/作品/20260920_标题_7001.mp4",
        legacy_work_orphan: "ghost/作品/20260920_标题_7002.mp4",
        legacy_clash: "阿B(抖音)/直播间/2026-09-22/clash.ts",
        legacy_dup: "阿B/直播间/2026-09-20/dup.ts",
        legacy_part: "小美/直播间/2026-09-20/小美_200101_part1_0001.ts",
    }
    for old, new in expect.items():
        check(not os.path.exists(os.path.join(OUT, old)), f"旧路径已清空 {old}")
        check(os.path.isfile(os.path.join(OUT, new)), f"新路径已就位 {new}")
    check(len(os.listdir(os.path.join(OUT, "阿B/直播间/2026-09-20"))) == 2,
          "同大小重复文件去重后不产生副本")
    check(not os.path.isdir(os.path.join(OUT, "works"))
          and not os.path.isdir(os.path.join(OUT, "B站"))
          and not os.path.isdir(os.path.join(OUT, "快手")), "旧平台根目录已被清理")

    async with async_session() as s:
        folders = [r.folder_name for r in (await s.execute(
            select(Room).order_by(Room.id))).scalars().all()]
        check(folders == ["小美", "阿B", "小美", "小美(快手)"], f"归档目录名分配 {folders}")
        recs = {r.file_path for r in (await s.execute(select(Recording))).scalars().all()}
        check(expect[legacy_live] in recs, "录制记录路径已回写")
        check(all(not p.startswith(("抖音/", "B站/", "快手/", "works/")) for p in recs),
              "无残留旧口径录制路径")
        work = (await s.execute(select(Work))).scalar_one()
        check(work.file_path == expect[legacy_work], f"作品路径已回写 {work.file_path}")
        parts = json.loads((await s.execute(
            select(Recording).where(Recording.part_paths.isnot(None))
        )).scalars().first().part_paths)
        check(parts[0] == "小美/直播间/2026-09-20/小美_200101_part1_%04d.ts",
              f"分段 part 模板同步 {parts[0]}")

    print("== 幂等重跑 ==")
    # 主播名正好是平台中文名时，新布局路径看起来也像旧布局，不得被再次搬迁
    platform_named = ("抖音/直播间/2026-09-23/新录.ts", "抖音/作品/20260923_标题_9.mp4")
    for rel in platform_named:
        touch(rel)
    again = await archive.migrate_legacy_layout()
    check(again["moved"] == 0 and again["records"] == 0 and again["works"] == 0,
          f"第二次迁移无操作 {again}")
    for rel in platform_named:
        check(os.path.isfile(os.path.join(OUT, rel)), f"平台同名主播文件未被误搬 {rel}")

    print("== 文件扫描 / 主播汇总 ==")
    file_manager.invalidate_cache("file_list")
    pmap = await archive.folder_platform_map()
    check(pmap.get("小美") == "抖音" and pmap.get("小美(快手)") == "快手", f"目录->平台映射 {pmap}")
    files = file_manager.get_file_list(platform_map=pmap)
    cat = {f["path"]: f["category"] for f in files}
    check(cat["小美/直播间/2026-09-20/小美_200101.ts"] == "live", "新布局直播归类")
    check(cat["小美/作品/20260920_标题_7001.mp4"] == "works", "新布局作品归类")
    plat = {f["path"]: f["platform"] for f in files}
    check(plat["小美/直播间/2026-09-20/小美_200101.ts"] == "抖音", "平台字段由映射补齐")
    check(len(file_manager.get_file_list(platform="抖音", platform_map=pmap)) == 4,
          "按平台筛选命中数")

    agg = {s["streamer"]: s for s in file_manager.get_streamers(platform_map=pmap)}
    check(agg["小美"]["live_count"] == 3 and agg["小美"]["works_count"] == 1, "主播分栏计数")
    check(agg["小美(快手)"]["live_count"] == 1 and agg["小美(快手)"]["platform"] == "快手",
          "跨平台同名主播单独成卡")
    check(agg["阿B"]["live_count"] == 2 and agg["阿B(抖音)"]["live_count"] == 1, "撞名归档分卡")
    check(agg["已注销"]["platform"] == "" and agg["ghost"]["works_count"] == 1, "无主归档保留")

    print("== 归档目录名分配 ==")
    async with async_session() as s:
        r1 = await s.get(Room, 1)
        r1.streamer_name = "小美改名"
        await archive.sync_folder_name(s, r1, display="小美改名")
        check(r1.folder_name == "小美", "已落盘主播改名后目录不漂移")

        r5 = Room(url="https://live.douyin.com/777", platform="douyin", room_id="777",
                  remark="新人")
        s.add(r5)
        await s.commit()
        await s.refresh(r5)
        await archive.sync_folder_name(s, r5)
        check(r5.folder_name == "新人", f"未落盘主播取显示名 {r5.folder_name}")
        s.add(Recording(room_id=r5.id, file_path="新人/直播间/2026-09-20/x.ts",
                        file_name="x.ts", status="completed"))
        await s.commit()
        await archive.sync_folder_name(s, r5, display="真名")
        check(r5.folder_name == "新人", "落盘后目录名固定")

        r6 = Room(url="https://live.douyin.com/888", platform="douyin", room_id="888",
                  streamer_name="小美")
        s.add(r6)
        await s.commit()
        await s.refresh(r6)
        await archive.sync_folder_name(s, r6)
        check(r6.folder_name == "小美", "同平台同主播共用归档目录")

    print("== 录制 / 作品写入路径 ==")
    final_path, part_target = recorder.build_session_target(
        "douyin", "小美", "123", base_dir=archive.live_root(r1), part_index=1,
        record_format="ts", segment_time=300, template="{streamer}_{time}", title="标题")
    check(final_path.startswith(os.path.join(OUT, "小美", "直播间") + os.sep),
          f"直播录像落新目录 {os.path.relpath(final_path, OUT)}")
    check("_part001_%04d.ts" in part_target, f"分段 part {os.path.basename(part_target)}")
    check(archive.works_root(r1) == os.path.join(OUT, "小美", "作品"), "作品落 主播/作品")
    check(archive.live_root(r1) == os.path.join(OUT, "小美", "直播间"), "直播落 主播/直播间")

    print("== 手动合并产物归位 ==")
    frag_a = "小美/直播间/2026-09-20/fused_a.mp4"
    frag_b = "小美/直播间/2026-09-20/fused_b.mp4"
    if shutil.which("ffmpeg"):
        import subprocess
        for rel in (frag_a, frag_b):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=duration=0.2:size=128x96:rate=10",
                 "-pix_fmt", "yuv420p", "-c:v", "libx264", os.path.join(OUT, rel)],
                check=True, stdin=subprocess.DEVNULL)
        merged = file_manager.merge_recordings([frag_a, frag_b], "mp4")
        check(merged.get("success") is True, f"合并成功 {merged.get('error', '')[:80]}")
        check(os.path.dirname(merged["output_rel"]) == os.path.dirname(frag_a),
              f"合并产物落在首个输入所在归档目录 {merged['output_rel']}")
        check(os.path.isfile(os.path.join(OUT, frag_a)), "合并不删除输入碎片")
    else:
        print("  skip  未安装 ffmpeg，跳过合并产物归位检查")

    merged_dir_files = [f for f in os.listdir(OUT) if os.path.isdir(os.path.join(OUT, f))]
    check("merged" not in merged_dir_files, "不再产生无主的 merged/ 根目录")

    shutil.rmtree(BASE, ignore_errors=True)
    print("\n" + ("全部通过" if not FAILED else f"{len(FAILED)} 项失败: {FAILED}"))
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
