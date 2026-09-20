"""通用小工具：统一时间口径

全项目数据库时间列一律存 naive UTC（避免容器/宿主时区差异导致时长与排序错乱）；
API 序列化时补 +00:00 偏移，浏览器 new Date() 才能正确换算为本地时间显示。
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """naive UTC 当前时间（所有 DateTime 列写入统一用此函数）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime):
    """naive UTC datetime -> 带 +00:00 偏移的 ISO 字符串；None 透传"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
