"""Bilibili直播适配器"""
import re
import json
import logging
from typing import Optional
from app.services.platform.base import BasePlatform, RoomInfo, PlatformFactory

logger = logging.getLogger(__name__)


@PlatformFactory.register
class BilibiliPlatform(BasePlatform):
    """Bilibili直播适配器"""

    platform_name = "bilibili"

    # 画质映射
    QUALITY_MAP = {
        "origin": 10000,
        "blue_ray": 400,
        "ultra": 10000,
        "high": 250,
        "medium": 150,
        "low": 80,
    }

    def match_url(self, url: str) -> bool:
        return any(domain in url for domain in ["live.bilibili.com", "bilibili.com"])

    def extract_room_id(self, url: str) -> str:
        patterns = [
            r"live\.bilibili\.com/(\d+)",
            r"live\.bilibili\.com/h5/(\d+)",
            r"live\.bilibili\.com/blanc/(\d+)",
            r"room_id=(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    async def get_room_info(self, url: str) -> RoomInfo:
        """获取B站直播间信息"""
        info = RoomInfo(platform="bilibili")
        room_id = self.extract_room_id(url)

        if not room_id:
            logger.error(f"无法从URL提取B站房间ID: {url}")
            return info

        info.room_id = room_id

        try:
            # 获取直播间信息
            room_info_url = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={room_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://live.bilibili.com/{room_id}",
                "Accept": "application/json, text/plain, */*",
            }
            guest_cookie = await self._ensure_guest_cookie()
            if guest_cookie:
                headers["Cookie"] = guest_cookie

            response = await self.client.get(room_info_url, headers=headers)
            data = response.json()

            if data.get("code") == 0:
                room_data = data.get("data", {}).get("room_info", {})
                info.title = room_data.get("title", "")
                info.is_live = room_data.get("live_status", 0) == 1
                info.cover_url = room_data.get("cover", "")
                info.streamer_name = room_data.get("uname", "")

                if info.is_live:
                    info.stream_url = await self._get_stream_url(room_id)

            logger.info(f"B站房间 {room_id}: 标题={info.title}, 直播中={info.is_live}")

        except Exception as e:
            logger.error(f"获取B站房间信息失败 {url}: {e}")

        return info

    async def _ensure_guest_cookie(self) -> str:
        """游客 Cookie：优先使用系统配置的 bilibili_cookie（方案B），否则自动获取真实 buvid3（方案A，默认）。

        B站裸请求会被风控拦截（code=-352），必须带一个真实有效的 buvid3 游客标识才能拿到流地址。
        buvid3 来自访问 bilibili.com 首页时服务端下发的 Set-Cookie，无需登录。
        结果缓存在 self._buvid3，避免每次请求都重新获取。
        """
        if self.cookie:
            return self.cookie
        cached = getattr(self, "_buvid3", "")
        if cached:
            return f"buvid3={cached}"
        try:
            resp = await self.client.get(
                "https://www.bilibili.com/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            buvid3 = resp.cookies.get("buvid3", "")
            if buvid3:
                self._buvid3 = buvid3
                logger.info("B站自动获取游客 buvid3 成功（绕过风控）")
                return f"buvid3={self._buvid3}"
        except Exception as e:
            logger.warning(f"B站自动获取 buvid3 失败: {e}")
        return ""

    async def _get_stream_url(self, room_id: str) -> str:
        """获取B站直播流地址"""
        try:
            quality = 10000  # 原画
            stream_url = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"

            params = {
                "room_id": room_id,
                "protocol": "0,1",
                "format": "0,1,2",
                "codec": "0,1",
                "qn": quality,
                "platform": "web",
                "ptype": 16,
                "dolby": 5,
                "panorama": 1,
            }

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://live.bilibili.com/{room_id}",
                "Accept": "application/json, text/plain, */*",
            }
            guest_cookie = await self._ensure_guest_cookie()
            if guest_cookie:
                headers["Cookie"] = guest_cookie

            response = await self.client.get(stream_url, params=params, headers=headers)
            data = response.json()

            if data.get("code") == 0:
                playurl_info = data.get("data", {}).get("playurl_info", {})
                playurl = playurl_info.get("playurl", {})
                streams = playurl.get("stream", [])

                for stream in streams:
                    protocol = stream.get("protocol_name", "")
                    format_list = stream.get("format", [])

                    for fmt in format_list:
                        codec_list = fmt.get("codec", [])
                        for codec in codec_list:
                            url_list = codec.get("url_info", [])
                            base_url = codec.get("base_url", "")
                            host = ""

                            for url_info in url_list:
                                host = url_info.get("host", "")
                                if host:
                                    break

                            if host and base_url:
                                extra = codec.get("url_info", [{}])[0].get("extra", "")
                                full_url = host + base_url + extra
                                if protocol == "flv" or "flv" in base_url:
                                    return full_url

                # 如果没找到FLV，尝试HLS
                for stream in streams:
                    if stream.get("protocol_name") == "http_hls" or stream.get("protocol_name") == "hls":
                        format_list = stream.get("format", [])
                        for fmt in format_list:
                            codec_list = fmt.get("codec", [])
                            for codec in codec_list:
                                url_list = codec.get("url_info", [])
                                base_url = codec.get("base_url", "")
                                for url_info in url_list:
                                    host = url_info.get("host", "")
                                    if host and base_url:
                                        extra = url_info.get("extra", "")
                                        return host + base_url + extra

        except Exception as e:
            logger.error(f"获取B站直播流地址失败: {e}")

        return ""
