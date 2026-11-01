"""Live Recorder - 多平台直播录制平台"""
import logging
import os
import shutil
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text
from app.database import init_db, async_session
from app.config import settings, get_data_dir
from app.routers import rooms, recordings, system, files, works, collections
from app.services.archive import migrate_legacy_layout
from app.services.monitor import monitor
from app.services.works_monitor import works_monitor
from app.services.sync_service import sync_service

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


def _check_persistent_mounts():
    """启动自检：输出/同步/**数据**目录必须是容器内挂载点（卷/bind mount）。

    若用户在设置里把 output_dir/sync_root 改成未映射路径，数据会写进容器
    可写层——功能全部正常但重建容器即静默丢失。这里在启动时明确告警。

    P1-24: 原自检只查 output_dir 与 sync_root，**漏了数据库所在的数据目录** ——
    `./data` 若漏挂，SQLite 落容器可写层，重启即丢全部主播/录制记录/作品记录，
    比丢录像更严重。数据目录必须一并检查。
    """
    data_dir = get_data_dir(settings.database_url)
    checks = [
        ("录制输出目录", settings.output_dir),
        ("数据目录(数据库)", data_dir),
    ]
    if settings.sync_enabled and settings.sync_root:
        checks.append(("NAS 同步根目录", settings.sync_root))
    for label, path in checks:
        try:
            if os.path.isdir(path) and not os.path.ismount(path):
                logger.warning(
                    f"⚠️ {label} {path} 不是容器内挂载点！数据将写入容器可写层，"
                    f"重建容器即全部丢失。请在 docker-compose/Container Station "
                    f"把 NAS 目录以卷形式挂载到该路径（named volume 或 bind mount）。"
                )
        except OSError:
            pass


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
    _check_persistent_mounts()

    # 存量归档：把旧布局（平台/主播/日期、works/平台/主播）的文件搬进 主播/{直播间,作品}
    # 并回写 DB 路径。必须在各监控启动前完成，后续写入才会用新目录。
    await migrate_legacy_layout()

    await monitor.start()
    logger.info("监控调度器已启动")

    await works_monitor.start()
    logger.info("作品订阅监控已启动")

    await sync_service.start()
    logger.info("NAS 同步服务已启动")

    logger.info("平台启动完成，等待请求...")

    yield

    # 关闭
    logger.info("正在关闭...")
    await sync_service.stop()
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
app.include_router(collections.router)

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    """主页"""
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/api/health")
async def health():
    """健康检查（P1-24：真实探针，不再是静态常量）

    原实现直接返回 {"status":"ok"}，不查 DB、不查 ffmpeg、不查后台 task 是否存活。
    于是 ffmpeg 被管道阻塞、监控循环异常退出、DB 不可写时，容器仍报 healthy ——
    编排层/QNAP UI 全绿但录制管线已死，restart: unless-stopped 也不会自愈。

    这里做三项最小检查；任一项失败返回 503（Docker healthcheck 与 compose 依赖据此判定）。
    """
    problems = []

    # 1) 数据库可读（SELECT 1，开销极小）
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        problems.append(f"数据库不可用: {e}")

    # 2) ffmpeg 可执行（录制管线的唯一硬依赖）
    if shutil.which("ffmpeg") is None:
        problems.append("ffmpeg 不可执行（PATH 中找不到）")

    # 3) 后台服务 task 存活（monitor / works_monitor / sync_service）
    for name, task in (
        ("直播监控", monitor._task),
        ("作品监控", works_monitor._task),
        ("NAS 同步", sync_service._task),
    ):
        if task is not None and task.done():
            problems.append(f"{name}后台任务已退出")

    if problems:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "live-recorder", "problems": problems},
        )
    return {"status": "ok", "service": "live-recorder"}
