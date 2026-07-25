"""抖音直播适配器"""
import re
import json
import hashlib
import time
import logging
from typing import Optional
from app.services.platform.base import BasePlatform, RoomInfo, PlatformFactory

logger = logging.getLogger(__name__)


@PlatformFactory.register
class DouyinPlatform(BasePlatform):
    """抖音直播适配器"""

    platform_name = "douyin"

    def match_url(self, url: str) -> bool:
        return any(domain in url for domain in ["live.douyin.com", "douyin.com", "iesdouyin.com"])

    def extract_room_id(self, url: str) -> str:
        patterns = [
            r"live\.douyin\.com/(\d+)",
            r"live\.douyin\.com/([a-zA-Z0-9]+)",
            r"room_id=(\d+)",
            r"douyin\.com/(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    async def get_room_info(self, url: str) -> RoomInfo:
        """获取抖音直播间信息"""
        info = RoomInfo(platform="douyin")
        room_id = self.extract_room_id(url)

        if not room_id:
            logger.error(f"无法从URL提取抖音房间ID: {url}")
            return info

        info.room_id = room_id

        try:
            live_url = f"https://live.douyin.com/{room_id}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://live.douyin.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie

            response = await self.client.get(live_url, headers=headers)
            text = response.text

            # 从页面中提取渲染数据
            render_data_match = re.search(
                r'<script id="RENDER_DATA"[^>]*>(.*?)</script>', text, re.DOTALL
            )

            if render_data_match:
                from urllib.parse import unquote
                render_data = unquote(render_data_match.group(1))
                data = json.loads(render_data)

                # 解析直播数据 - 路径可能随版本变化
                live_data = None
                for key, value in data.items():
                    if isinstance(value, dict):
                        if "room" in value or "liveRoom" in value:
                            live_data = value
                            break
                    if isinstance(value, str):
                        try:
                            inner = json.loads(value)
                            if isinstance(inner, dict) and ("room" in inner or "liveRoom" in inner):
                                live_data = inner
                                break
                        except json.JSONDecodeError:
                            pass

                if live_data:
                    room = live_data.get("room", live_data.get("liveRoom", {}))
                    info.title = room.get("title", "")
                    info.is_live = room.get("status", 0) == 2

                    owner = room.get("owner", {})
                    info.streamer_name = owner.get("nickname", "")

                    stream_url = room.get("stream_url", {})
                    info.stream_url = self._extract_flv_stream(stream_url)

                    if not info.stream_url:
                        info.stream_url = self._extract_hls_stream(stream_url)

                    cover = room.get("cover", {})
                    if isinstance(cover, dict):
                        info.cover_url = cover.get("url_list", [""])[0] if cover.get("url_list") else ""

                    if info.is_live and not info.stream_url:
                        info.stream_url = await self._get_stream_from_api(room_id)

            else:
                # 尝试通过API获取
                info.stream_url = await self._get_stream_from_api(room_id)
                if info.stream_url:
                    info.is_live = True

            logger.info(f"抖音房间 {room_id}: 标题={info.title}, 直播中={info.is_live}, 有流地址={bool(info.stream_url)}")

        except Exception as e:
            logger.error(f"获取抖音房间信息失败 {url}: {e}")

        return info

    async def _get_stream_from_api(self, room_id: str) -> str:
        """通过API获取直播流地址"""
        try:
            ttwid = await self._get_ttwid()
            ms_token = await self._get_ms_token()

            api_url = "https://live.douyin.com/webcast/room/web/enter/"
            params = {
                "aid": "6383",
                "app_name": "douyin_web",
                "device_platform": "web",
                "enter_from": "web_live",
                "cookie_enabled": "true",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Chrome",
                "browser_version": "120.0.0.0",
                "web_rid": room_id,
                "enter_source": "",
                "Room-Enter-User-Login-Ab": "0",
                "is_need_double_stream": "false",
                "insert_task_id": "",
                "live_reason": "",
            }

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://live.douyin.com/{room_id}",
                "Accept": "application/json, text/plain, */*",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            if ttwid:
                headers["Cookie"] = headers.get("Cookie", "") + f"; ttwid={ttwid}"
            if ms_token:
                params["msToken"] = ms_token
                headers["X-Bogus"] = self._get_x_bogus(params)

            response = await self.client.get(api_url, headers=headers, params=params)
            data = response.json()

            if data.get("status_code") == 0:
                room_data = data.get("data", {}).get("data", [{}])[0]
                if room_data:
                    stream_url_data = room_data.get("stream_url", {})
                    return self._extract_flv_stream(stream_url_data) or self._extract_hls_stream(stream_url_data)

        except Exception as e:
            logger.error(f"API获取抖音流地址失败: {e}")

        return ""

    async def _get_ttwid(self) -> str:
        """获取ttwid cookie"""
        try:
            response = await self.client.post(
                "https://ttwid.bytedance.com/ttwid/union/register/",
                json={
                    "region": "cn",
                    "aid": 1768,
                    "needFid": False,
                    "service": "www.douyin.com",
                    "migrate_info": {"ticket": "", "source": "node"},
                    "cbUrlProtocol": "https",
                    "union": True,
                },
            )
            cookies = response.cookies
            ttwid = cookies.get("ttwid", "")
            if ttwid:
                return ttwid
        except Exception:
            pass
        return ""

    async def _get_ms_token(self) -> str:
        """生成msToken"""
        import random
        import string
        chars = string.ascii_letters + string.digits + "=_"
        return "".join(random.choice(chars) for _ in range(107))

    def _get_x_bogus(self, params: dict) -> str:
        """生成X-Bogus签名 (简化版)"""
        import base64
        import struct

        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        timestamp = int(time.time())

        data = query.encode("utf-8")
        hash_val = hashlib.md5(data).hexdigest()

        bogus_base = f"{hash_val}{timestamp}"
        bogus_bytes = bogus_base.encode("utf-8")[:16]

        try:
            result = base64.b64encode(bogus_bytes).decode("utf-8")
            return result[:28]
        except Exception:
            return "DFSzswVOsQX78StNBWeM"

    def _extract_flv_stream(self, stream_url_data: dict) -> str:
        """提取FLV直播流地址"""
        try:
            flv_url = stream_url_data.get("rtmp_pull_url", "")
            if flv_url:
                return flv_url

            flv_pull_data = stream_url_data.get("flv_pull_url", {})
            if flv_pull_data:
                qualities = ["FULL_HD1", "HD1", "SD1", "SD2"]
                for q in qualities:
                    if q in flv_pull_data:
                        urls = flv_pull_data[q]
                        if isinstance(urls, list) and urls:
                            return urls[0]
                        elif isinstance(urls, str):
                            return urls

            pull_data = stream_url_data.get("pull_data", {})
            if pull_data and "stream" in pull_data:
                return pull_data["stream"]

        except Exception as e:
            logger.error(f"提取FLV流地址失败: {e}")

        return ""

    def _extract_hls_stream(self, stream_url_data: dict) -> str:
        """提取HLS直播流地址"""
        try:
            hls_url = stream_url_data.get("hls_pull_url", "")
            if hls_url:
                return hls_url

            hls_pull_data = stream_url_data.get("hls_pull_url_params", "")
            if hls_pull_data and hls_url:
                return f"{hls_url}?{hls_pull_data}"

            flv_pull_data = stream_url_data.get("flv_pull_url", {})
            if flv_pull_data:
                for key, value in flv_pull_data.items():
                    url = value[0] if isinstance(value, list) else value
                    if url and ".flv" in url:
                        return url.replace(".flv", ".m3u8")

        except Exception as e:
            logger.error(f"提取HLS流地址失败: {e}")

        return ""
