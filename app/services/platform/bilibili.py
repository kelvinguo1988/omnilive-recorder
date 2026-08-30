"""Bilibili直播适配器"""
import re
import json
import time
import hashlib
import logging
from typing import Optional
from urllib.parse import urlencode
from app.services.platform.base import (
    BasePlatform, RoomInfo, PlatformFactory, UserInfo, WorkInfo,
    WORKS_UA, WORKS_UA_CHROME_VER,
)

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

    @classmethod
    def match_url(cls, url: str) -> bool:
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
                # 主播 uid：直播间↔主页互通的关键（作品订阅/主页地址解析都靠它）
                if room_data.get("uid"):
                    info.owner_user_id = str(room_data["uid"])

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

    # ---------- 作品订阅 ----------

    # wbi 签名混淆表（来源: SocialSisterYi/bilibili-API-collect docs/misc/sign/wbi.md）
    _WBI_MIXIN_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
        26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
        20, 34, 44, 52,
    ]

    @classmethod
    def extract_user_id(cls, url: str) -> str:
        """从主页URL提取 mid（形如 https://space.bilibili.com/123456）"""
        m = re.search(r"space\.bilibili\.com/(\d+)", url)
        return m.group(1) if m else ""

    async def _get_wbi_keys(self):
        """获取 wbi 签名 img/sub key（游客可用，缓存 1 小时）"""
        cached = getattr(self, "_wbi_cache", None)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        headers = {
            "User-Agent": WORKS_UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json",
        }
        cookie = await self._ensure_guest_cookie()
        if cookie:
            headers["Cookie"] = cookie
        resp = await self.client.get("https://api.bilibili.com/x/web-interface/nav", headers=headers)
        data = resp.json()
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError(f"获取 wbi key 失败: code={data.get('code')}")
        self._wbi_cache = (time.time(), (img_key, sub_key))
        return img_key, sub_key

    async def _wbi_sign(self, params: dict) -> dict:
        """wbi 签名：参数排序拼接后 md5，附加 wts/w_rid"""
        img_key, sub_key = await self._get_wbi_keys()
        mixin = "".join((img_key + sub_key)[i] for i in self._WBI_MIXIN_TAB)[:32]
        signed = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
                  for k, v in sorted({**params, "wts": int(time.time())}.items())}
        qs = urlencode(signed)
        signed["w_rid"] = hashlib.md5((qs + mixin).encode()).hexdigest()
        return signed

    async def _api_headers(self, referer: str = "https://www.bilibili.com/") -> dict:
        headers = {
            "User-Agent": WORKS_UA,
            "Referer": referer,
            "Accept": "application/json",
        }
        cookie = await self._ensure_guest_cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def get_user_info(self, user_id: str) -> UserInfo:
        info = UserInfo(user_id=user_id)
        if not user_id:
            return info
        try:
            # card 接口无需 wbi 签名，游客可用
            resp = await self.client.get(
                "https://api.bilibili.com/x/web-interface/card",
                params={"mid": user_id, "photo": "true"},
                headers=await self._api_headers(),
            )
            data = resp.json()
            if data.get("code") == 0:
                card = (data.get("data") or {}).get("card") or {}
                info.nickname = card.get("name") or ""
                info.avatar_url = card.get("face") or ""
            else:
                logger.warning(f"B站用户信息接口异常: code={data.get('code')} {data.get('message', '')}")
        except Exception as e:
            logger.error(f"获取B站用户信息失败 {user_id}: {e}")
        return info

    async def get_user_works(self, user_id: str, cursor: int = 0, count: int = 20):
        """分页获取投稿视频。

        用 series/recArchivesByKeywords（keywords 为空即全部视频，无需 wbi/登录，
        无 arc/search 的 -412 风控）。cursor 即页码（从 0 起，内部转为 1 起）。
        """
        works = []
        page = max(int(cursor), 0) + 1
        try:
            resp = await self.client.get(
                "https://api.bilibili.com/x/series/recArchivesByKeywords",
                params={
                    "mid": user_id,
                    "keywords": "",
                    "ps": str(min(max(count, 1), 100)),
                    "pn": str(page),
                    "orderby": "pubdate",
                },
                headers=await self._api_headers(f"https://space.bilibili.com/{user_id}/video"),
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(
                    f"B站作品列表接口失败: code={data.get('code')} {data.get('message', '')}"
                )
            archives = (data.get("data") or {}).get("archives") or []
            for item in archives:
                w = WorkInfo()
                w.work_id = item.get("bvid") or ""
                w.title = item.get("title") or ""
                w.publish_ts = int(item.get("pubdate") or item.get("ctime") or 0)
                w.duration = float(item.get("duration") or 0)
                w.cover_url = item.get("pic") or ""
                if w.work_id:
                    works.append(w)
            has_more = len(archives) >= min(max(count, 1), 100)
            return works, page, has_more
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"获取B站作品列表失败 {user_id}: {e}")
            raise RuntimeError(f"获取B站作品列表失败: {e}") from e

    async def get_download_urls(self, work: WorkInfo) -> list:
        """解析投稿视频直链（列表接口不含地址，下载时二次解析）。

        游客态 platform=html5 + fnval=0 拿渐进式 MP4（清晰度受限但可直接下载）；
        配置了 bilibili_cookie 时可用更高 qn。
        """
        if work.download_urls:
            return work.download_urls
        if not work.work_id:
            return []
        try:
            headers = await self._api_headers()
            view = await self.client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": work.work_id},
                headers=headers,
            )
            vdata = view.json()
            if vdata.get("code") != 0:
                logger.warning(f"B站视频信息获取失败 {work.work_id}: {vdata.get('message', '')}")
                return []
            v = vdata.get("data") or {}
            aid, cid = v.get("aid"), v.get("cid")
            if not aid or not cid:
                return []

            signed = await self._wbi_sign({
                "avid": str(aid), "cid": str(cid), "qn": "64",
                "fnval": "0", "fnver": "0",
                "platform": "html5", "high_quality": "1",
            })
            play = await self.client.get(
                "https://api.bilibili.com/x/player/wbi/playurl",
                params=signed,
                headers=headers,
            )
            pdata = play.json()
            if pdata.get("code") != 0:
                logger.warning(f"B站播放地址获取失败 {work.work_id}: {pdata.get('message', '')}")
                return []
            durl = (pdata.get("data") or {}).get("durl") or []
            return [d["url"] for d in durl if isinstance(d, dict) and d.get("url")]
        except Exception as e:
            logger.error(f"解析B站下载地址失败 {work.work_id}: {e}")
            return []

    async def find_room_id_by_user(self, user_id: str) -> str:
        """由 mid 解析直播间房间号。

        B站直播短号机制：getRoomPlayInfo 的 room_id 参数可直接传 mid，
        code=0 时 data.room_id 即真实房间号；60004 表示该 UP 没有直播间。
        """
        if not user_id:
            return ""
        try:
            resp = await self.client.get(
                "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
                params={
                    "room_id": user_id, "protocol": "0,1", "format": "0,1,2",
                    "codec": "0,1", "qn": 10000, "platform": "web", "ptype": 16,
                },
                headers=await self._api_headers("https://live.bilibili.com/"),
            )
            data = resp.json()
            if data.get("code") == 0:
                room_id = str((data.get("data") or {}).get("room_id") or "")
                return room_id
            # 60004 = 用户没有直播间，属正常情况，静默返回
            return ""
        except Exception as e:
            logger.warning(f"B站 mid→直播间解析失败 {user_id}: {e}")
            return ""
