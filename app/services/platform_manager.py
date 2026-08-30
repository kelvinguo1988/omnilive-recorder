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
            self._instances[platform_name] = instance
        return self._instances.get(platform_name)

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
