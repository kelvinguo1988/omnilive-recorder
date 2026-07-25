"""Live Recorder - 多平台直播录制平台"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.config import settings
from app.routers import rooms, recordings, system, files
from app.services.monitor import monitor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动
    logger.info("=" * 50)
    logger.info("直播录制平台启动中...")
    logger.info(f"输出目录: {settings.output_dir}")
    logger.info(f"录制格式: {settings.record_format}")
    logger.info(f"监控间隔: {settings.monitor_interval}s")
    logger.info("=" * 50)

    await init_db()
    logger.info("数据库初始化完成")

    await monitor.start()
    logger.info("监控调度器已启动")

    logger.info("平台启动完成，等待请求...")

    yield

    # 关闭
    logger.info("正在关闭...")
    await monitor.stop()
    logger.info("平台已关闭")


app = FastAPI(
    title="Live Recorder",
    description="多平台直播录制平台 - 支持抖音/Bilibili/快手",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(rooms.router)
app.include_router(recordings.router)
app.include_router(system.router)
app.include_router(files.router)

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    """主页"""
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "live-recorder"}
