"""平台适配器基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import httpx
import logging

logger = logging.getLogger(__name__)

# 作品订阅统一 UA：抖音 a_bogus 签名与请求必须使用同一 UA；其余平台沿用以保持一致
WORKS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WORKS_UA_CHROME_VER = "131.0.0.0"

# 平台中文名（展示/目录命名统一取此映射）
PLATFORM_CN = {"douyin": "抖音", "bilibili": "B站", "kuaishou": "快手"}


# ---------------------------------------------------------------- 错误分类
# 作品订阅拉取失败时，必须区分「Cookie 失效」与「网络抖动/风控限流」：
# 前者需要用户重新配置 Cookie（且应立即停止重试，否则持续撞风控），
# 后者只需退避等待。原实现全部抛裸 RuntimeError，上层无从判断。

class PlatformError(RuntimeError):
    """平台请求失败基类"""

    #: 是否为凭据（Cookie/登录态）失效
    cookie_invalid: bool = False


class CookieInvalidError(PlatformError):
    """Cookie 失效/缺失 —— 需要用户重新配置，不应重试"""

    cookie_invalid = True


class RateLimitedError(PlatformError):
    """被风控限流（-412/403/429）—— 退避后可能恢复，不是 Cookie 问题"""

    cookie_invalid = False


# 各平台的「Cookie 失效」特征串（小写匹配响应体/错误信息）
_COOKIE_INVALID_MARKERS = (
    "uifid not found",          # 抖音 ArgusSecurityPlugin：无登录态设备指纹
    "blocked by argussecurity",
    "请先登录",
    "未登录",
    "not login",
    "login required",
    "invalid session",
    "session expired",
    "登录已过期",
    "cookie 已失效",
    "cookie已失效",
)

# 各平台的「被风控限流」特征串
_RATE_LIMIT_MARKERS = (
    "request was banned",       # B站 -412
    "too many requests",
    "429",
    "频繁",
)


def classify_platform_error(message: str, status_code: int = 0) -> PlatformError:
    """根据响应特征判断错误类型，返回相应的结构化异常。

    用于把「一串看不懂的报错」翻译成「Cookie 失效」或「被限流」，
    上层据此决定是告警用户还是仅退避。
    """
    text = (message or "").lower()

    if status_code in (401,):
        return CookieInvalidError(message)
    if any(m in text for m in _COOKIE_INVALID_MARKERS):
        return CookieInvalidError(message)
    if status_code == 403 and any(m in text for m in ("blocked", "forbidden", "uifid")):
        return CookieInvalidError(message)
    if status_code == 429 or any(m in text for m in _RATE_LIMIT_MARKERS):
        return RateLimitedError(message)
    # 403 但没有明确特征：抖音/B站 的 403 基本都是登录态问题
    if status_code == 403:
        return CookieInvalidError(message)
    return PlatformError(message)


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
    # 主播平台用户ID(sec_uid/mid/principal)：直播间↔主页互通的关键，
    # 检测直播间时回填，之后无需主页地址即可订阅作品
    owner_user_id: str = ""
    # 探测是否可信：False=本次探测失败（网络/风控/解析异常/接口返回空数据），
    # 此时 is_live 的值没有意义，调用方不得据此判断"下播"并结束场次，
    # 否则一次网络抖动就会被当成下播，把同一直播切成多段（P0-8）。
    probe_ok: bool = True


@dataclass
class UserInfo:
    """创作者主页信息"""
    user_id: str = ""
    nickname: str = ""
    avatar_url: str = ""


@dataclass
class WorkInfo:
    """单条作品信息（视频或图集）

    download_urls: 视频为 1 个无水印地址；图集为 N 个图片地址（顺序）。
    publish_ts: 发布时间 unix 秒（未知为 0）。

    合集/多P 相关字段（仅「按合集下载」使用，普通作品订阅时保持默认值）：
      episode_index  集号（从 1 起）；0 = 不属于任何合集
      collection_key 合集去重键，如 bili:season:415241 / bili:multipart:BV1xx
      collection_title 合集名称
      part_index     平台内的分P号（B站 ?p=N）；0 = 无分P概念
    """
    work_id: str = ""
    work_type: str = "video"
    title: str = ""
    publish_ts: int = 0
    duration: float = 0
    download_urls: list = field(default_factory=list)
    cover_url: str = ""
    episode_index: int = 0
    collection_key: str = ""
    collection_title: str = ""
    part_index: int = 0


@dataclass
class CollectionInfo:
    """一个「合集 / 多P / 单视频」的解析结果

    kind: collection(真合集, 跨BV) / multipart(同一BV的多个分P) / single(单视频)
    """
    kind: str = "single"
    title: str = ""
    collection_key: str = ""
    items: list = field(default_factory=list)   # list[WorkInfo]
    platform: str = ""
    total: int = 0


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

    @classmethod
    @abstractmethod
    def extract_room_id(cls, url: str) -> str:
        """从URL中提取房间ID（纯URL解析，免实例调用）"""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def match_url(cls, url: str) -> bool:
        """判断URL是否属于当前平台（纯URL判断，不依赖实例状态，供 detect_platform 免实例调用）"""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def extract_user_id(cls, url: str) -> str:
        """从创作者主页URL提取平台用户ID（sec_uid/mid/eid）"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_info(self, user_id: str) -> UserInfo:
        """获取创作者昵称/头像"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_works(self, user_id: str, cursor: int = 0, count: int = 20):
        """分页获取创作者作品列表。

        返回 (works: list[WorkInfo], next_cursor: int, has_more: bool)。
        cursor 语义由各平台自定（时间戳/页码），调用方原样回传。
        """
        raise NotImplementedError

    async def get_download_urls(self, work: WorkInfo) -> list:
        """解析作品下载地址。

        列表接口已返回直链的平台（抖音/快手）默认直接返回；
        B站等需二次解析的平台在子类覆写（可能触发额外 API 调用）。
        work 只有 work_id/work_type/title 可靠填充（来自 DB 记录）。
        """
        return work.download_urls

    async def find_room_id_by_user(self, user_id: str) -> str:
        """由主播平台用户ID解析直播间房间号（主页→直播间桥接）。

        B站可用 getRoomPlayInfo 短号机制；抖音/快手需登录 Cookie。
        解析失败返回空串，不抛异常。
        """
        return ""

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
    def get_platform_class(cls, platform_name: str) -> Optional[type]:
        """获取平台适配器类（用于免实例调用 extract_user_id 等 classmethod）"""
        return cls._platforms.get(platform_name)

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
