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
                ("sync_path", "VARCHAR(200)"),
            ]:
                if col not in room_cols:
                    sync_conn.execute(text(f"ALTER TABLE rooms ADD COLUMN {col} {ddl}"))

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
                    # 同平台同ID的主播已存在则并入，否则新建主播行（无直播间地址）
                    existing = sync_conn.execute(
                        text("SELECT id FROM rooms WHERE platform=:p AND platform_user_id=:u "
                             "AND platform_user_id != ''"),
                        {"p": platform, "u": puid},
                    ).first()
                    if existing:
                        new_id = existing[0]
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
