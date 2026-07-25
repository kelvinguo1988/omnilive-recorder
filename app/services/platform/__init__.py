"""平台适配器模块"""
from app.services.platform.base import BasePlatform, RoomInfo, PlatformFactory
from app.services.platform.douyin import DouyinPlatform
from app.services.platform.bilibili import BilibiliPlatform
from app.services.platform.kuaishou import KuaishouPlatform

__all__ = ["BasePlatform", "RoomInfo", "PlatformFactory",
           "DouyinPlatform", "BilibiliPlatform", "KuaishouPlatform"]
