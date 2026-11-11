"""Cookie 健康状态追踪

解决「Cookie 什么时候失效了不知道」的痛点：
- 每次作品拉取成功后记一次「验证通过」
- 识别到 Cookie 失效（Uifid Not Found / 401 / 未登录 等）时标记失效，
  并**暂停该平台的自动拉取**，避免持续撞风控把账号打进黑名单
- 通过 API 暴露给前端，在仪表盘/主播页显示告警

状态只保存在内存（进程级），不落库：
- 它是「运行时观测结果」，不是配置；重启后重新验证即可
- 落库反而会带来「旧状态误导用户」的问题

注意：Cookie 失效 ≠ 被限流。
  失效 -> 需要用户重新配置（立即告警，暂停拉取）
  限流 -> 退避等待即可（不告警，交给 works_monitor 的指数退避）
"""
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 状态取值
STATUS_UNKNOWN = "unknown"      # 尚未验证过
STATUS_OK = "ok"                # 最近一次验证通过
STATUS_INVALID = "invalid"      # 最近一次验证明确失败（Cookie 问题）
STATUS_RATE_LIMITED = "rate_limited"  # 被限流（Cookie 可能没问题）

# 失效后暂停该平台自动拉取的时长（秒）：避免持续撞风控
INVALID_PAUSE_SECONDS = 3600


class CookieHealth:
    """单个平台的 Cookie 健康状态"""

    def __init__(self, platform: str):
        self.platform = platform
        self.status = STATUS_UNKNOWN
        self.last_ok_ts: float = 0.0
        self.last_fail_ts: float = 0.0
        self.last_error: str = ""
        self.fail_count: int = 0
        # 失效后到该时间点之前不再自动拉取
        self.paused_until: float = 0.0

    def mark_ok(self):
        self.status = STATUS_OK
        self.last_ok_ts = time.time()
        self.fail_count = 0
        self.last_error = ""
        self.paused_until = 0.0

    def mark_invalid(self, error: str):
        """标记 Cookie 失效：记录原因并暂停自动拉取"""
        self.status = STATUS_INVALID
        self.last_fail_ts = time.time()
        self.fail_count += 1
        self.last_error = (error or "")[:300]
        self.paused_until = time.time() + INVALID_PAUSE_SECONDS
        logger.error(
            f"[{self.platform}] Cookie 已失效，暂停自动拉取 "
            f"{INVALID_PAUSE_SECONDS // 60} 分钟：{self.last_error[:120]}"
        )

    def mark_rate_limited(self, error: str):
        """标记被限流：不暂停（交给 works_monitor 的指数退避），仅记录"""
        self.status = STATUS_RATE_LIMITED
        self.last_fail_ts = time.time()
        self.last_error = (error or "")[:300]
        # 限流不清空 last_ok，说明 Cookie 本身可能是好的

    def is_paused(self) -> bool:
        return time.time() < self.paused_until

    def to_dict(self) -> dict:
        now = time.time()

        def ago(ts: float) -> Optional[int]:
            return int(now - ts) if ts else None

        return {
            "platform": self.platform,
            "status": self.status,
            "status_text": {
                STATUS_UNKNOWN: "未验证",
                STATUS_OK: "正常",
                STATUS_INVALID: "已失效",
                STATUS_RATE_LIMITED: "被限流",
            }.get(self.status, self.status),
            "last_ok_ago": ago(self.last_ok_ts),
            "last_fail_ago": ago(self.last_fail_ts),
            "fail_count": self.fail_count,
            "last_error": self.last_error,
            "paused": self.is_paused(),
            "paused_remaining": (
                int(self.paused_until - now) if self.is_paused() else 0
            ),
            "needs_action": self.status == STATUS_INVALID,
        }


class CookieHealthRegistry:
    """各平台 Cookie 健康状态集合（进程级单例）"""

    def __init__(self):
        self._items: dict = {}

    def get(self, platform: str) -> CookieHealth:
        item = self._items.get(platform)
        if item is None:
            item = CookieHealth(platform)
            self._items[platform] = item
        return item

    def mark_ok(self, platform: str):
        self.get(platform).mark_ok()

    def mark_invalid(self, platform: str, error: str):
        self.get(platform).mark_invalid(error)

    def mark_rate_limited(self, platform: str, error: str):
        self.get(platform).mark_rate_limited(error)

    def is_paused(self, platform: str) -> bool:
        """该平台是否因 Cookie 失效而暂停自动拉取"""
        return self.get(platform).is_paused()

    def any_needs_action(self) -> bool:
        return any(i.status == STATUS_INVALID for i in self._items.values())

    def snapshot(self) -> list:
        """所有平台状态（含未出现过的平台，保证前端列表完整）"""
        from app.services.platform.base import PLATFORM_CN
        out = []
        for name in PLATFORM_CN:
            out.append(self.get(name).to_dict())
        return out


cookie_health = CookieHealthRegistry()
