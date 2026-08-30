"""快手直播适配器"""
import re
import json
import logging
from typing import Optional
from app.services.platform.base import (
    BasePlatform, RoomInfo, PlatformFactory, UserInfo, WorkInfo, WORKS_UA,
)

logger = logging.getLogger(__name__)


@PlatformFactory.register
class KuaishouPlatform(BasePlatform):
    """快手直播适配器"""

    platform_name = "kuaishou"

    @classmethod
    def match_url(cls, url: str) -> bool:
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
            # 必须使用原直播间 URL（含 /u/ 路径），拼成 live.kuaishou.com/{id}
            # 会落到错误页面，__INITIAL_STATE__ 无 liveroom 节点，永远取不到直播状态
            live_url = f"https://live.kuaishou.com/u/{room_id}"

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
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;', text, re.DOTALL
            ) or re.search(
                r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;', text, re.DOTALL
            )

            if apollo_match:
                try:
                    raw = apollo_match.group(1)
                    # 快手注水含 JS 字面量（undefined/NaN/Infinity），非法 JSON，预处理为 null
                    raw = re.sub(r':\s*undefined\b', ': null', raw)
                    raw = re.sub(r':\s*(NaN|Infinity|-Infinity)\b', ': null', raw)
                    apollo_data = json.loads(raw)
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
                        # 快手 playUrls 结构随版本变化，需兼容多种形态
                        play_urls = stream.get("playUrls") or detail.get("playUrls")
                        # 形态1: dict（h264/hevc -> adaptationSet.representation[].url）
                        if isinstance(play_urls, dict):
                            for codec in ("h264", "hevc"):
                                node = play_urls.get(codec)
                                if not isinstance(node, dict):
                                    continue
                                rep_set = node.get("adaptationSet", {})
                                if isinstance(rep_set, dict):
                                    for r in rep_set.get("representation", []):
                                        if isinstance(r, dict) and r.get("url"):
                                            info.stream_url = r["url"]
                                            break
                                if info.stream_url:
                                    break
                        # 形态2: list（旧版 urls[].url）
                        if not info.stream_url and isinstance(play_urls, list):
                            for item in play_urls:
                                if not isinstance(item, dict):
                                    continue
                                for u in item.get("urls", []):
                                    if isinstance(u, dict) and u.get("url"):
                                        info.stream_url = u["url"]
                                        break
                                    if isinstance(u, str):
                                        info.stream_url = u
                                        break
                                if info.stream_url:
                                    break
                        # 形态3: liveStream 直接字段兜底
                        if not info.stream_url:
                            for key in ("hlsPlayUrl", "url"):
                                direct = stream.get(key)
                                if isinstance(direct, str) and direct.startswith("http"):
                                    info.stream_url = direct
                                    break
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

            # 完全没解析到任何主播/直播信息：区分"无Cookie(游客被风控)"与"Cookie失效"
            if not info.streamer_name and not info.is_live and not info.stream_url:
                if not self.cookie:
                    logger.warning(
                        f"快手房间 {room_id}: 未配置Cookie(游客态被风控), 无法检测直播/主播信息。"
                        f"请在「系统设置 → 快手 Cookie」填写登录态 Cookie 后重试。"
                    )
                else:
                    logger.warning(
                        f"快手房间 {room_id}: 已带Cookie但仍未解析到任何信息, "
                        f"可能Cookie已过期或主播当前未开播。可重新登录快手获取新Cookie后更新。"
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

    # ---------- 作品订阅 ----------

    @classmethod
    def extract_user_id(cls, url: str) -> str:
        """从主页URL提取用户ID（形如 https://www.kuaishou.com/profile/xxx）"""
        m = re.search(r"kuaishou\.com/profile/([\w.-]+)", url)
        return m.group(1) if m else ""

    async def _ks_graphql(self, operation: str, query: str, variables: dict, referer: str):
        """快手主站 GraphQL 请求。

        游客首次调用前先 GET 一次主页拿 did 等游客 Cookie（httpx cookie jar 自动携带）；
        配置了 kuaishou_cookie 时以手动 Cookie 头优先。
        """
        headers = {
            "User-Agent": WORKS_UA,
            "Referer": referer,
            "Origin": "https://www.kuaishou.com",
            "Accept": "*/*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        else:
            # 预热游客 Cookie（仅一次）
            if not getattr(self, "_ks_warmed", False):
                try:
                    await self.client.get(referer, headers={"User-Agent": WORKS_UA, "Accept": "text/html"})
                    self._ks_warmed = True
                except Exception:
                    pass
        payload = {"operationName": operation, "query": query, "variables": variables}
        resp = await self.client.post("https://www.kuaishou.com/graphql", json=payload, headers=headers)
        return resp.json()

    async def get_user_info(self, user_id: str) -> UserInfo:
        info = UserInfo(user_id=user_id)
        if not user_id:
            return info
        referer = f"https://www.kuaishou.com/profile/{user_id}"
        try:
            data = await self._ks_graphql(
                "visionProfile",
                """query visionProfile($userId: String) {
                    visionProfile(userId: $userId) {
                        result
                        userPhoto { id name headUrl }
                    }
                }""",
                {"userId": user_id},
                referer,
            )
            prof = (data.get("data") or {}).get("visionProfile") or {}
            if prof.get("result") == 1:
                up = prof.get("userPhoto") or {}
                info.nickname = up.get("name") or ""
                info.avatar_url = up.get("headUrl") or ""
            else:
                logger.warning(
                    f"快手用户信息接口异常: result={prof.get('result')} "
                    f"(游客被风控时需配置快手Cookie)"
                )
        except Exception as e:
            logger.error(f"获取快手用户信息失败 {user_id}: {e}")
        return info

    # 图集字段（images/atlas）随版本存在性不定：先带全量字段查询，
    # graphql 报字段不存在时降级为基础字段并记住
    _PHOTO_LIST_QUERY_FULL = """query visionProfilePhotoList($userId: String, $page: Int, $webPageArea: String) {
        visionProfilePhotoList(userId: $userId, page: $page, webPageArea: $webPageArea) {
            result
            feeds {
                type
                photo {
                    id
                    caption
                    duration
                    timestamp
                    photoUrl
                    coverUrls { url }
                    images { url }
                    atlas { images }
                }
            }
            pcursor
        }
    }"""
    _PHOTO_LIST_QUERY_BASIC = """query visionProfilePhotoList($userId: String, $page: Int, $webPageArea: String) {
        visionProfilePhotoList(userId: $userId, page: $page, webPageArea: $webPageArea) {
            result
            feeds {
                type
                photo {
                    id
                    caption
                    duration
                    timestamp
                    photoUrl
                    coverUrls { url }
                }
            }
            pcursor
        }
    }"""

    async def get_user_works(self, user_id: str, cursor: int = 0, count: int = 20):
        """分页获取作品。cursor 为页码-1（内部 page=cursor+1），pcursor 控制终止。"""
        works = []
        page = max(int(cursor), 0) + 1
        referer = f"https://www.kuaishou.com/profile/{user_id}"
        try:
            query = self._PHOTO_LIST_QUERY_BASIC if getattr(self, "_ks_basic_query", False) \
                else self._PHOTO_LIST_QUERY_FULL
            data = await self._ks_graphql(
                "visionProfilePhotoList", query,
                {"userId": user_id, "page": page, "webPageArea": "home"},
                referer,
            )
            # graphql 字段不存在等错误 → 降级基础字段重试一次
            errors = data.get("errors")
            if errors and not getattr(self, "_ks_basic_query", False):
                msgs = " ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))
                if "images" in msgs or "atlas" in msgs:
                    self._ks_basic_query = True
                    data = await self._ks_graphql(
                        "visionProfilePhotoList", self._PHOTO_LIST_QUERY_BASIC,
                        {"userId": user_id, "page": page, "webPageArea": "home"},
                        referer,
                    )

            node = (data.get("data") or {}).get("visionProfilePhotoList") or {}
            if node.get("result") not in (1, None):
                raise RuntimeError(
                    f"快手作品列表接口失败: result={node.get('result')} "
                    f"(游客被风控时需配置快手Cookie)"
                )

            for feed in node.get("feeds") or []:
                photo = (feed or {}).get("photo") or {}
                w = self._photo_to_work(photo)
                if w and w.work_id:
                    works.append(w)

            pcursor = str(node.get("pcursor") or "")
            has_more = bool(works) and pcursor not in ("no_more", "", "None")
            return works, page, has_more
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"获取快手作品列表失败 {user_id}: {e}")
            raise RuntimeError(f"获取快手作品列表失败: {e}") from e

    @staticmethod
    def _photo_to_work(photo: dict) -> Optional[WorkInfo]:
        if not isinstance(photo, dict):
            return None
        w = WorkInfo()
        w.work_id = str(photo.get("id") or "")
        w.title = photo.get("caption") or ""
        ts = int(photo.get("timestamp") or 0)
        if ts > 10**12:  # 毫秒时间戳归一为秒
            ts //= 1000
        w.publish_ts = ts
        dur = photo.get("duration") or 0
        w.duration = dur / 1000.0 if dur > 2000 else float(dur)

        # 图集: images[{url}] 或 atlas{images:[[url,...],...]}
        img_urls = []
        images = photo.get("images")
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get("url"):
                    img_urls.append(img["url"])
                elif isinstance(img, str) and img.startswith("http"):
                    img_urls.append(img)
        atlas = photo.get("atlas") or {}
        if isinstance(atlas, dict):
            for group in atlas.get("images") or []:
                if isinstance(group, list) and group:
                    first = group[0]
                    if isinstance(first, str) and first.startswith("http"):
                        img_urls.append(first)
        if img_urls:
            w.work_type = "images"
            w.duration = 0
            w.download_urls = img_urls
            return w

        url = photo.get("photoUrl") or ""
        if isinstance(url, str) and url.startswith("//"):
            url = "https:" + url
        if url:
            w.download_urls = [url]
        covers = photo.get("coverUrls") or []
        if covers and isinstance(covers[0], dict):
            w.cover_url = covers[0].get("url") or ""
        return w
