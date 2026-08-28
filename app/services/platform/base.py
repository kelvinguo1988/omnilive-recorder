"""平台适配器基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import httpx
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class RoomInfo:
    """直播间信息"""
    room_id: str = ""
    title: str = ""
    streamer_name: str = ""
    is_live: bool = False
    stream_url: str = ""
    cover_url: str = ""
    platform: str = ""


class BasePlatform(ABC):
    """平台适配器基类"""

    platform_name: str = "unknown"

    def __init__(self, proxy: str = "", cookie: str = "", timeout: int = 15):
        self.proxy = proxy
        # Cookie 可能从 UI 粘贴带入首尾空白/换行，httpx 拒绝含换行符的 header 值，统一在此清理
        self.cookie = cookie.strip() if cookie else ""
        self.timeout = timeout
        # 优化点：显式设置连接池上限，避免长期运行下连接数无限增长（观察项 #5）
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=True,
            proxy=proxy if proxy else None,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self):
        await self.client.aclose()

    @abstractmethod
    async def get_room_info(self, url: str) -> RoomInfo:
        """获取直播间信息"""
        pass

    @abstractmethod
    def extract_room_id(self, url: str) -> str:
        """从URL中提取房间ID"""
        pass

    @classmethod
    @abstractmethod
    def match_url(cls, url: str) -> bool:
        """判断URL是否属于当前平台（纯URL判断，不依赖实例状态，供 detect_platform 免实例调用）"""
        raise NotImplementedError

    async def _fetch(self, url: str, headers: dict = None, params: dict = None) -> httpx.Response:
        """发送HTTP请求"""
        default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie:
            default_headers["Cookie"] = self.cookie
        if headers:
            default_headers.update(headers)

        response = await self.client.get(url, headers=default_headers, params=params)
        return response

    async def _post(self, url: str, headers: dict = None, json_data: dict = None, params: dict = None) -> httpx.Response:
        """发送POST请求"""
        default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie:
            default_headers["Cookie"] = self.cookie
        if headers:
            default_headers.update(headers)

        response = await self.client.post(url, headers=default_headers, json=json_data, params=params)
        return response


class PlatformFactory:
    """平台适配器工厂"""

    _platforms: dict = {}

    @classmethod
    def register(cls, platform_class: type):
        """注册平台适配器"""
        cls._platforms[platform_class.platform_name] = platform_class
        return platform_class

    @classmethod
    def get_platform(cls, platform_name: str, proxy: str = "", cookie: str = "", timeout: int = 15) -> Optional[BasePlatform]:
        """获取平台适配器实例"""
        platform_class = cls._platforms.get(platform_name)
        if platform_class:
            return platform_class(proxy=proxy, cookie=cookie, timeout=timeout)
        return None

    @classmethod
    def detect_platform(cls, url: str) -> Optional[str]:
        """根据URL自动检测平台

        match_url 为纯 URL 判断（classmethod），直接在类上调用；
        不再实例化适配器——构造函数会创建 httpx.AsyncClient，原实现在
        添加/导入/改URL 等高频路径上每次泄漏未关闭的 client。
        """
        for name, platform_class in cls._platforms.items():
            if platform_class.match_url(url):
                return name
        return None

    @classmethod
    def get_all_platforms(cls) -> list:
        """获取所有已注册平台"""
        return list(cls._platforms.keys())
