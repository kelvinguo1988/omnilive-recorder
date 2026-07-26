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

        # 增量迁移：为 recordings 表补充 part_paths 列（已存在则忽略）
        def _migrate(sync_conn):
            from sqlalchemy import text
            cols = [r[1] for r in sync_conn.execute(
                text("PRAGMA table_info(recordings)")
            ).fetchall()]
            if "part_paths" not in cols:
                sync_conn.execute(
                    text("ALTER TABLE recordings ADD COLUMN part_paths TEXT")
                )

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
