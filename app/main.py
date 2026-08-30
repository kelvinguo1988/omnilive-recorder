"""Live Recorder - 多平台直播录制平台"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.database import init_db
from app.config import settings
from app.routers import rooms, recordings, system, files, works
from app.services.monitor import monitor
from app.services.works_monitor import works_monitor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# P1-3: 可选的 API 鉴权。设置环境变量 LIVE_RECORDER_API_TOKEN 后，所有 /api/* 请求
# 必须携带 `Authorization: Bearer <token>`；未设置则该中间件不生效（向后兼容）。
API_TOKEN = os.environ.get("LIVE_RECORDER_API_TOKEN", "").strip()


class AuthMiddleware(BaseHTTPMiddleware):
    """轻量 Bearer Token 鉴权（仅当配置了 API_TOKEN 时启用）。"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        # 健康检查免鉴权
        if path == "/api/health":
            return await call_next(request)
        # 仅对 /api/* 鉴权；静态首页与 / 放行
        if API_TOKEN and path.startswith("/api/"):
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {API_TOKEN}":
                return JSONResponse(status_code=401, content={"detail": "未授权：缺少或错误的 API Token"})
        return await call_next(request)


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

    await works_monitor.start()
    logger.info("作品订阅监控已启动")

    logger.info("平台启动完成，等待请求...")

    yield

    # 关闭
    logger.info("正在关闭...")
    await works_monitor.stop()
    await monitor.stop()
    logger.info("平台已关闭")


app = FastAPI(
    title="Live Recorder",
    description="多平台直播录制平台 - 支持抖音/Bilibili/快手",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
# P1-3: 关闭 allow_credentials（与 allow_origins="*" 互斥，原组合违反浏览器规范）。
# 若需带凭据跨域，请把 allow_origins 改为具体的前端域名白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# P1-3: 可选 API 鉴权中间件（仅当配置了 LIVE_RECORDER_API_TOKEN 时生效）
if API_TOKEN:
    app.add_middleware(AuthMiddleware)
    logger.info("已启用 API Token 鉴权（LIVE_RECORDER_API_TOKEN）")
else:
    logger.warning(
        "未设置 LIVE_RECORDER_API_TOKEN，API 接口无鉴权。"
        "请仅在内网部署，或在反向代理前加鉴权，或设置该环境变量启用 Bearer Token 鉴权。"
    )

# 注册路由
app.include_router(rooms.router)
app.include_router(recordings.router)
app.include_router(system.router)
app.include_router(files.router)
app.include_router(works.router)

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
