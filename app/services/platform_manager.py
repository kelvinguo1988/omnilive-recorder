"""平台适配器统一管理层

直播监控(monitor)与作品监控(works_monitor)共享的基础设施：
- 按平台缓存适配器实例，Cookie/代理变化自动重建（先 close 旧实例防连接泄漏）
- 暴露 get/reset 两类操作；领域逻辑（怎么检测直播、怎么拉作品）分属各自监控模块
"""
import logging
from app.config import settings
from app.services.platform import PlatformFactory

logger = logging.getLogger(__name__)

# 平台 -> 对应配置 Cookie 字段
_PLATFORM_COOKIE_KEY = {
    "douyin": "douyin_cookie",
    "bilibili": "bilibili_cookie",
    "kuaishou": "kuaishou_cookie",
}


def _current_loop_token():
    """当前事件循环的标识；无运行中的循环时返回 None"""
    import asyncio
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class PlatformManager:
    """Cookie/代理感知的平台适配器实例缓存"""

    def __init__(self):
        self._instances: dict = {}

    def _cookie_for(self, platform_name: str) -> str:
        key = _PLATFORM_COOKIE_KEY.get(platform_name)
        return (getattr(settings, key, "") or "") if key else ""

    def _proxy_for(self) -> str:
        return settings.proxy_addr if settings.enable_proxy else ""

    async def get(self, platform_name: str):
        """获取平台适配器实例；Cookie/代理变化时自动重建"""
        cookie = self._cookie_for(platform_name)
        proxy = self._proxy_for()

        cached = self._instances.get(platform_name)
        # 事件循环变更检测：httpx.AsyncClient 的连接池绑定在创建它的事件循环上。
        # 若缓存实例来自另一个（已关闭的）循环，复用它会在下一个请求抛
        # "Event loop is closed"。生产环境 uvicorn 单循环不会遇到，但测试框架
        # （TestClient 每请求一循环）、--reload、以及将来若改成多循环运行时会踩到。
        # 这里显式比对循环归属，失效则静默重建，不打断调用方。
        if cached is not None and not self._same_loop(cached):
            self._instances.pop(platform_name, None)
            cached = None

        if cached is not None and getattr(cached, "cookie", None) == cookie \
                and getattr(cached, "proxy", None) == proxy:
            return cached

        if cached is not None:
            try:
                await cached.close()
            except Exception:
                pass

        instance = PlatformFactory.get_platform(
            platform_name, proxy=proxy, cookie=cookie, timeout=settings.check_timeout,
        )
        if instance:
            # 记录创建时的事件循环，供下次 get() 判断是否可安全复用
            try:
                instance._loop_token = _current_loop_token()
            except Exception:
                pass
            self._instances[platform_name] = instance
        return self._instances.get(platform_name)

    @staticmethod
    def _same_loop(instance) -> bool:
        """缓存实例是否仍属当前事件循环"""
        try:
            cached_loop = getattr(instance, "_loop_token", None)
            return cached_loop is None or cached_loop is _current_loop_token()
        except Exception:
            return False

    async def reset(self):
        """关闭并清空全部实例（修改 Cookie/代理/地址后调用）"""
        for inst in list(self._instances.values()):
            try:
                await inst.close()
            except Exception:
                pass
        self._instances.clear()


# 全局单例
platform_manager = PlatformManager()
