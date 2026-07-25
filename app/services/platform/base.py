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
        self.cookie = cookie
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=True,
            proxy=proxy if proxy else None,
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

    @abstractmethod
    def match_url(self, url: str) -> bool:
        """判断URL是否属于当前平台"""
        pass

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
        instance_keys = platform_class.platform_name
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
        """根据URL自动检测平台"""
        for name, platform_class in cls._platforms.items():
            instance = platform_class()
            if instance.match_url(url):
                return name
        return None

    @classmethod
    def get_all_platforms(cls) -> list:
        """获取所有已注册平台"""
        return list(cls._platforms.keys())
