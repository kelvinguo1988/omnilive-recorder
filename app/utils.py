"""通用小工具：统一时间口径

全项目数据库时间列一律存 naive UTC（避免容器/宿主时区差异导致时长与排序错乱）；
API 序列化时补 +00:00 偏移，浏览器 new Date() 才能正确换算为本地时间显示。

**目录/文件名用本地时区**（观众看到的日期应是主播开播的本地日期），统一走 localnow()，
不要再在业务代码里裸调 datetime.now() —— 容器没设 TZ 时会退回 UTC，导致日期目录整体偏 8h。
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """naive UTC 当前时间（所有 DateTime 列写入统一用此函数）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def localnow() -> datetime:
    """当前**本地时区**时间（仅用于归档目录名/文件名，绝不用于数据库列）。

    依赖 TZ 环境变量：Dockerfile 已固定 ENV TZ=Asia/Shanghai，compose 也显式传 TZ。
    若两者都缺失（例如裸 docker run 未带 -e TZ），容器默认 UTC，这里会退回 UTC ——
    这是可接受的降级（时间仍然单调、不产生重复目录），但请在部署时设置 TZ。
    """
    return datetime.now()


def iso(dt: datetime):
    """naive UTC datetime -> 带 +00:00 偏移的 ISO 字符串；None 透传"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
