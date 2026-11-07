"""数据库连接管理"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.models import Base
from app.config import settings, get_data_dir
import os


# 确保数据目录存在
os.makedirs(get_data_dir(settings.database_url), exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    """初始化数据库表（含存量库增量迁移）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 增量迁移
        def _migrate(sync_conn):
            from sqlalchemy import text
            insp_cols = lambda table: [r[1] for r in sync_conn.execute(
                text(f"PRAGMA table_info({table})")
            ).fetchall()]

            # recordings 表补充 part_paths 列
            cols = insp_cols("recordings")
            if "part_paths" not in cols:
                sync_conn.execute(
                    text("ALTER TABLE recordings ADD COLUMN part_paths TEXT")
                )

            # rooms 表升级为统一主播实体：补充主页/作品订阅列
            room_cols = insp_cols("rooms")
            for col, ddl in [
                ("home_url", "VARCHAR(500)"),
                ("platform_user_id", "VARCHAR(150)"),
                ("works_enabled", "BOOLEAN DEFAULT 0"),
                ("backfill_done", "BOOLEAN DEFAULT 0"),
                ("last_work_check_time", "DATETIME"),
                ("folder_name", "VARCHAR(150)"),
            ]:
                if col not in room_cols:
                    sync_conn.execute(text(f"ALTER TABLE rooms ADD COLUMN {col} {ddl}"))

            # works 表补充合集/多P 支持列（幂等：已存在则跳过）
            work_cols = insp_cols("works")
            for col, ddl in [
                ("collection_key", "VARCHAR(200)"),
                ("collection_title", "VARCHAR(500)"),
                ("episode_index", "INTEGER"),
            ]:
                if col not in work_cols:
                    sync_conn.execute(text(f"ALTER TABLE works ADD COLUMN {col} {ddl}"))
            # 合集查询索引：create_all 只对新库建索引，存量库需显式补（IF NOT EXISTS 保证幂等）
            sync_conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_works_collection_key ON works (collection_key)"
            ))

            # 统一主播模型迁移：旧 creators 表数据合并进 rooms 后删除
            tables = [r[0] for r in sync_conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()]
            if "creators" in tables and "works" in tables:
                creator_rows = sync_conn.execute(
                    text("SELECT id, platform, platform_user_id, nickname, home_url, "
                         "enabled, backfill_done, remark FROM creators")
                ).fetchall()
                for cid, platform, puid, nickname, home_url, enabled, backfilled, remark in creator_rows:
                    # 查重匹配（逐级放宽）：
                    # a) platform_user_id —— 存量 rooms 行该列是本次迁移才加的、全为 NULL，
                    #    `!= ''` 对 NULL 恒不成立，不能作为唯一条件（否则每个创作者都插成
                    #    新行，与已有直播间的同一主播重复）；
                    # b) home_url 相同；
                    # c) 同平台且主播名相同且该行未开作品订阅（老库无法离线验证
                    #    直播间与主页是否同一人，同名启发式仅在未占用作品订阅时合并）。
                    existing = sync_conn.execute(
                        text("SELECT id FROM rooms WHERE platform=:p AND "
                             "((platform_user_id IS NOT NULL AND platform_user_id != '' AND "
                             "  platform_user_id=:u) OR home_url=:h OR "
                             " (works_enabled=0 AND streamer_name IS NOT NULL AND streamer_name=:n))"),
                        {"p": platform, "u": puid, "h": home_url or "", "n": nickname or ""},
                    ).first()
                    if existing:
                        new_id = existing[0]
                        # 合并时必须把作品订阅字段带到已有主播行，否则 works_enabled
                        # 保持 0，works_monitor 过滤 works_enabled 时会静默跳过
                        sync_conn.execute(
                            text("UPDATE rooms SET "
                                 "home_url=COALESCE(NULLIF(home_url,''), :h), "
                                 "platform_user_id=COALESCE(NULLIF(platform_user_id,''), :u), "
                                 "works_enabled=1, backfill_done=:bd, "
                                 "streamer_name=COALESCE(NULLIF(streamer_name,''), :n) "
                                 "WHERE id=:id"),
                            {"h": home_url, "u": puid, "bd": bool(backfilled),
                             "n": nickname, "id": new_id},
                        )
                    else:
                        ins = sync_conn.execute(
                            text("INSERT INTO rooms (url, platform, quality, enabled, is_live, "
                                 "is_recording, home_url, platform_user_id, works_enabled, "
                                 "backfill_done, streamer_name, remark, created_at) "
                                 "VALUES ('', :p, 'origin', :e, 0, 0, :h, :u, :we, :bd, :n, :r, "
                                 "datetime('now'))"),
                            {"p": platform, "e": bool(enabled), "h": home_url, "u": puid,
                             "we": True, "bd": bool(backfilled), "n": nickname, "r": remark},
                        )
                        new_id = ins.lastrowid
                    sync_conn.execute(
                        text("UPDATE works SET creator_id=:n WHERE creator_id=:o"),
                        {"n": new_id, "o": cid},
                    )
                sync_conn.execute(text("DROP TABLE creators"))

        await conn.run_sync(_migrate)


async def get_db():
    """获取数据库会话"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
