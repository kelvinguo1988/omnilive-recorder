"""快手直播适配器"""
import re
import json
import logging
from typing import Optional
from app.services.platform.base import BasePlatform, RoomInfo, PlatformFactory

logger = logging.getLogger(__name__)


@PlatformFactory.register
class KuaishouPlatform(BasePlatform):
    """快手直播适配器"""

    platform_name = "kuaishou"

    def match_url(self, url: str) -> bool:
        return any(domain in url for domain in ["live.kuaishou.com", "kuaishou.com", "kwai.com"])

    def extract_room_id(self, url: str) -> str:
        patterns = [
            r"live\.kuaishou\.com/u/(\w+)",
            r"live\.kuaishou\.com/(\w+)",
            r"live\.kuaishou\.com/(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    async def get_room_info(self, url: str) -> RoomInfo:
        """获取快手直播间信息"""
        info = RoomInfo(platform="kuaishou")
        room_id = self.extract_room_id(url)

        if not room_id:
            logger.error(f"无法从URL提取快手房间ID: {url}")
            return info

        info.room_id = room_id

        try:
            live_url = f"https://live.kuaishou.com/{room_id}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://live.kuaishou.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie

            response = await self.client.get(live_url, headers=headers)
            text = response.text

            # 快手新版页面数据在 window.__INITIAL_STATE__（旧版为 __APOLLO_STATE__）
            apollo_match = re.search(
                r'window\.__INITIAL_STATE__\s*=\s*({.*?});', text, re.DOTALL
            ) or re.search(
                r'window\.__APOLLO_STATE__\s*=\s*({.*?});', text, re.DOTALL
            )

            if apollo_match:
                try:
                    apollo_data = json.loads(apollo_match.group(1))
                except json.JSONDecodeError:
                    apollo_data = {}

                # 新版结构: liveroom.playList[0]（登录态下含水播真实状态）
                detail = None
                lr = apollo_data.get("liveroom", {})
                if isinstance(lr, dict):
                    play_list = lr.get("playList", [])
                    if play_list and isinstance(play_list[0], dict):
                        detail = play_list[0]

                if detail:
                    info.is_live = bool(detail.get("isLiving", False))
                    stream = detail.get("liveStream", {}) or {}
                    if isinstance(stream, dict):
                        info.title = stream.get("caption") or stream.get("title") \
                            or detail.get("liveStreamName", "") or ""
                        cover = stream.get("coverUrl") or detail.get("coverUrl")
                        info.cover_url = cover.get("url", "") if isinstance(cover, dict) else (cover or "")
                        play_urls = stream.get("playUrls") or detail.get("playUrls")
                        if play_urls and isinstance(play_urls, list):
                            first = play_urls[0]
                            urls = first.get("urls", []) if isinstance(first, dict) else []
                            if urls:
                                info.stream_url = urls[0].get("url", "") if isinstance(urls[0], dict) else urls[0]
                    author = detail.get("author", {}) or {}
                    if isinstance(author, dict):
                        info.streamer_name = author.get("name", "") or author.get("kwaiId", "")

                # 旧版结构兜底: ROOT_QUERY -> liveDetail
                if not detail:
                    root_query = apollo_data.get("ROOT_QUERY", {})
                    for key, value in root_query.items():
                        if "live" in key.lower() and "detail" in key.lower():
                            if isinstance(value, dict) and "__ref" in value:
                                ref_key = value["__ref"]
                                live_detail = apollo_data.get(ref_key, {})

                                info.is_live = live_detail.get("isLiving", False) or live_detail.get("isLiving", 0) == 1
                                info.title = live_detail.get("liveStreamName", "") or live_detail.get("title", "")

                                streamer = live_detail.get("user", {})
                                if isinstance(streamer, dict):
                                    info.streamer_name = streamer.get("name", "") or streamer.get("kwaiId", "")

                                play_urls = live_detail.get("playUrls", [])
                                if play_urls:
                                    first_play = play_urls[0]
                                    if isinstance(first_play, dict):
                                        urls = first_play.get("urls", [])
                                        if urls:
                                            info.stream_url = urls[0].get("url", "")

                                cover = live_detail.get("coverUrl", {})
                                if isinstance(cover, dict):
                                    info.cover_url = cover.get("url", "")

                                break

            # 页面未拿到直播流时，尝试 GraphQL API（需登录 Cookie，游客态会被风控）
            if not info.stream_url and not info.is_live:
                info = await self._get_info_from_api(room_id, info)

            # 游客态接口已被快手风控拦截时，给出明确提示
            if not info.stream_url and not info.is_live:
                logger.warning(
                    f"快手房间 {room_id} 未获取到直播状态：游客态接口已被快手风控拦截。"
                    f"请在「系统设置 → 快手 Cookie」填写登录态 Cookie 后重试。"
                )

            logger.info(f"快手房间 {room_id}: 标题={info.title}, 直播中={info.is_live}")

        except Exception as e:
            logger.error(f"获取快手房间信息失败 {url}: {e}")

        return info

    async def _get_info_from_api(self, room_id: str, info: RoomInfo) -> RoomInfo:
        """通过GraphQL API获取快手直播间信息"""
        try:
            graphql_url = "https://live.kuaishou.com/live_graphql"

            query = {
                "operationName": "LiveDetail",
                "query": """query LiveDetail($principalId: String) {
                    liveDetail(principalId: $principalId) {
                        liveStream {
                            title
                            coverUrl
                            playUrls {
                                urls {
                                    url
                                }
                            }
                        }
                        user {
                            name
                            kwaiId
                        }
                        isLiving
                    }
                }""",
                "variables": {"principalId": room_id}
            }

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://live.kuaishou.com/{room_id}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://live.kuaishou.com",
            }
            if self.cookie:
                headers["Cookie"] = self.cookie

            response = await self.client.post(graphql_url, json=query, headers=headers)
            try:
                data = response.json()
            except Exception:
                logger.warning("快手 GraphQL 返回非 JSON，可能被风控拦截")
                return info

            # 风控拦截: 顶层 result!=0 或 message 含风控提示 -> 视为失败
            if data.get("result") not in (0, None) or "data" not in data:
                msg = data.get("message", "")
                logger.warning(f"快手 GraphQL 返回风控/失败: result={data.get('result')} message={msg!r}")
                return info

            live_detail = data.get("data", {}).get("liveDetail", {})
            if live_detail:
                info.is_live = live_detail.get("isLiving", False)
                info.streamer_name = live_detail.get("user", {}).get("name", "")

                live_stream = live_detail.get("liveStream", {})
                if live_stream:
                    info.title = live_stream.get("title", "")
                    info.cover_url = live_stream.get("coverUrl", "")

                    play_urls = live_stream.get("playUrls", [])
                    if play_urls:
                        urls = play_urls[0].get("urls", [])
                        if urls:
                            info.stream_url = urls[0].get("url", "")

        except Exception as e:
            logger.error(f"GraphQL API获取快手房间信息失败: {e}")

        return info
