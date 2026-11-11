"""抖音直播适配器"""
import re
import logging
from typing import Optional
from app.services.platform.base import (
    BasePlatform, RoomInfo, PlatformFactory, UserInfo, WorkInfo,
    WORKS_UA, WORKS_UA_CHROME_VER, PlatformError, classify_platform_error,
)
from app.services.platform.vendor.abogus import ABogus, BrowserFingerprintGenerator

logger = logging.getLogger(__name__)

# a_bogus 签名器缓存（按 UA 键控，进程级复用）。
# P1-1: ABogus 构造时会随机生成浏览器指纹（屏宽/高/outer 尺寸），
# 若每次签名都新建实例，同一会话内指纹会逐请求漂移——真实浏览器不会这样，
# 这是抖音 -352/空列表的最常见诱因。指纹只生成一次，之后所有请求复用同一实例。
# 键控 UA 是必须的：a_bogus 会把 UA 加密进签名，请求头 UA 与签名 UA 必须完全一致。
#
# 指纹的浏览器类型也必须与 UA 一致：原实现固定用 generate_fingerprint("Edge")，
# 而项目里请求头 UA 声称的是 Chrome（WORKS_UA，Chrome/131）——
# 「Chrome UA + Edge 指纹」是典型的自相矛盾特征，同样会被风控识别。
_abogus_signers: dict = {}


def _ua_browser_type(user_agent: str) -> str:
    """从 UA 推断浏览器类型，供生成与之匹配的设备指纹"""
    ua = user_agent or ""
    if "Edg/" in ua:
        return "Edge"
    if "Firefox/" in ua:
        return "Firefox"
    if "Safari/" in ua and "Chrome/" not in ua:
        return "Safari"
    return "Chrome"


def _get_abogus_signer(user_agent: str) -> ABogus:
    """取（并按 UA 缓存）签名器实例。构造只发生一次，指纹不再逐请求漂移。"""
    signer = _abogus_signers.get(user_agent)
    if signer is None:
        browser_type = _ua_browser_type(user_agent)
        fp = BrowserFingerprintGenerator.generate_fingerprint(browser_type)
        signer = ABogus(fp=fp, user_agent=user_agent)
        _abogus_signers[user_agent] = signer
    return signer


@PlatformFactory.register
class DouyinPlatform(BasePlatform):
    """抖音直播适配器"""

    platform_name = "douyin"

    @classmethod
    def match_url(cls, url: str) -> bool:
        return any(domain in url for domain in ["live.douyin.com", "douyin.com", "iesdouyin.com"])

    @classmethod
    def extract_room_id(cls, url: str) -> str:
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
            # 解析不出房间号 = 本次没有可信的直播状态，标记探测失败（P0-8）
            info.probe_ok = False
            return info

        info.room_id = room_id

        try:
            # P2-5: 抖音已弃用 RENDER_DATA 服务端渲染（8/16 修复确认），游客态页面不再返回
            # 房间信息，统一走 webcast API 获取流地址与标题/主播名，避免走已失效的解析分支。
            info.stream_url = await self._get_stream_from_api(room_id, info)
            if info.stream_url and not info.is_live:
                # API 未明确返回 status 时的兜底：能拿到流地址即视为直播中
                info.is_live = True

            logger.info(
                f"抖音房间 {room_id}: 主播={info.streamer_name or '-'}, "
                f"标题={info.title or '-'}, 直播中={info.is_live}, 有流地址={bool(info.stream_url)}"
            )

            # 游客态（无 Cookie）无法获取主播名，给出明确提示，避免一直按房间ID命名
            if not info.streamer_name and not self.cookie:
                logger.warning(
                    f"抖音房间 {room_id} 未获取到主播名：游客态接口不返回 owner 昵称。"
                    f"请在「系统设置 → 抖音 Cookie」填写登录态 Cookie 后重试（与快手同理）。"
                )

        except Exception as e:
            logger.error(f"获取抖音房间信息失败 {url}: {e}")
            # 异常即探测失败：此时 is_live 仍是默认 False，不能当作"已下播"（P0-8）
            info.probe_ok = False

        return info

    async def _get_stream_from_api(self, room_id: str, info: Optional["RoomInfo"] = None) -> str:
        """通过API获取直播流地址，并回填标题/主播名（若传入 info）。

        说明：抖音游客态接口不返回 owner（主播昵称），仅登录态 Cookie 才会返回；
        标题字段游客态即可返回，因此无 Cookie 时标题也能拿到，主播名不行。
        """
        try:
            ttwid = await self._get_ttwid()

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
            # P0-9: 不再伪造 msToken / X-Bogus。
            # 原实现用 `md5(query)+timestamp` 拼 base64 造一个假 X-Bogus，且只要生成了
            # msToken 就必然把它塞进请求头。抖音服务端对**非法** X-Bogus 的处置比不传更严
            # （直接 status_code != 0 / 空 data），等于主动把自己判成机器人。
            # 游客态取直播间信息本就不需要这两个参数；需要签名的作品接口走 _sign_params
            # 里的真实 a_bogus（见 P1-1 的进程级签名器）。

            response = await self.client.get(api_url, headers=headers, params=params)
            data = response.json()

            # P0-8: 接口非 0 状态码（风控/参数失效）拿不到可信的直播状态，
            # 标记探测失败，调用方（monitor）据此跳过本轮状态迁移，
            # 不把一次风控/抖动当成"下播"而切断正在进行的录制。
            if data.get("status_code") != 0:
                logger.warning(
                    f"抖音 enter 接口异常: status_code={data.get('status_code')} "
                    f"{(data.get('status_msg') or '')[:120]}"
                )
                if info is not None:
                    info.probe_ok = False
                return ""

            room_data = data.get("data", {}).get("data", [{}])[0]
            if not room_data:
                # 返回了空 data（风控常见表现）：同样视为探测失败
                if info is not None:
                    info.probe_ok = False
                return ""

            if info is not None:
                # 标题：游客态接口也返回，直接回填
                if not info.title and room_data.get("title"):
                    info.title = room_data.get("title")
                # 主播名：仅登录态(带 Cookie)接口才返回 owner，游客态 owner 为空
                owner = room_data.get("owner") or {}
                if isinstance(owner, dict):
                    nick = owner.get("nickname", "")
                    if nick and not info.streamer_name:
                        info.streamer_name = nick
                    # 主播 sec_uid：直播间↔主页互通的关键（作品订阅依赖）
                    if owner.get("sec_uid"):
                        info.owner_user_id = owner["sec_uid"]
                # 以接口返回的直播状态为准（2=直播中）
                status = room_data.get("status")
                if status is not None:
                    info.is_live = status == 2

            stream_url_data = room_data.get("stream_url", {})
            stream_url = self._extract_flv_stream(stream_url_data) or self._extract_hls_stream(stream_url_data)
            if info is not None and not stream_url:
                # 既没有 status 字段又没有流地址：这条响应不构成可信的"未开播"证据，
                # 只有明确 status!=2 时才算确认下播（P0-8）
                if room_data.get("status") is None:
                    info.probe_ok = False
            return stream_url

        except Exception as e:
            logger.error(f"API获取抖音流地址失败: {e}")
            # 异常（超时/连接重置/JSON 解析失败）= 探测失败（P0-8）
            if info is not None:
                info.probe_ok = False

        return ""

    async def _get_ttwid(self) -> str:
        """获取ttwid cookie（缓存，避免每次请求重复注册）"""
        cached = getattr(self, "_ttwid_cache", None)
        if cached:
            return cached
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
                self._ttwid_cache = ttwid
                return ttwid
        except Exception:
            pass
        return ""

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

            flv_pull_data = stream_url_data.get("flv_pull_url", {})
            if flv_pull_data:
                for key, value in flv_pull_data.items():
                    url = value[0] if isinstance(value, list) else value
                    if url and ".flv" in url:
                        return url.replace(".flv", ".m3u8")

        except Exception as e:
            logger.error(f"提取HLS流地址失败: {e}")

        return ""

    # ---------- 作品订阅 ----------

    @classmethod
    def extract_user_id(cls, url: str) -> str:
        """从主页URL提取 sec_uid（形如 https://www.douyin.com/user/MS4wLjABAAAA...）"""
        m = re.search(r"douyin\.com/user/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else ""

    async def _works_headers(self) -> dict:
        """作品接口请求头。

        UA 必须与 a_bogus 签名时的 UA 一致；Cookie 合并 ttwid（游客态必备）。
        """
        cookie = self.cookie or ""
        ttwid = getattr(self, "_ttwid_cache", None)
        if ttwid is None:
            ttwid = await self._get_ttwid()
            self._ttwid_cache = ttwid or ""
        if ttwid and "ttwid=" not in cookie:
            cookie = (cookie + "; " if cookie else "") + f"ttwid={ttwid}"
        return {
            "User-Agent": WORKS_UA,
            "Referer": "https://www.douyin.com/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        }

    def _sign_params(self, params: dict) -> str:
        """对查询串做 a_bogus 签名，返回已附加 a_bogus 的完整参数串

        P1-1: 复用进程级签名器（见模块顶部 _get_abogus_signer），指纹只在首次构造时
        生成一次。原实现每次调用都 ABogus(user_agent=WORKS_UA) 新建实例，
        构造函数里的 random.randint 让屏宽/高/outer 尺寸逐请求漂移，
        同一会话的指纹前后不一致 —— 这是抖音 -352/空列表的高发诱因。
        """
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return _get_abogus_signer(WORKS_UA).generate_abogus(qs)[0]

    async def get_user_info(self, user_id: str) -> UserInfo:
        info = UserInfo(user_id=user_id)
        if not user_id:
            return info
        try:
            params = {
                "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
                "sec_user_id": user_id,
                "publish_video_strategy_type": "2", "source": "channel_pc_web",
                "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
                "browser_language": "zh-CN", "browser_platform": "Win32",
                "browser_name": "Chrome", "browser_version": WORKS_UA_CHROME_VER,
                "browser_online": "true", "engine_name": "Blink",
                "browser_version_full": WORKS_UA_CHROME_VER,
                "os_name": "Windows", "os_version": "10", "platform": "PC",
            }
            url = "https://www.douyin.com/aweme/v1/web/user/profile/other/?" + self._sign_params(params)
            resp = await self.client.get(url, headers=await self._works_headers())
            data = resp.json()
            if data.get("status_code") == 0:
                user = data.get("user") or {}
                info.nickname = user.get("nickname") or ""
                thumb = (user.get("avatar_thumb") or {}).get("url_list") or []
                if thumb:
                    info.avatar_url = thumb[0]
            else:
                logger.warning(
                    f"抖音用户信息接口异常: status_code={data.get('status_code')} "
                    f"{(data.get('status_msg') or '')[:120]}"
                )
        except Exception as e:
            logger.error(f"获取抖音用户信息失败 {user_id}: {e}")
        return info

    async def get_user_works(self, user_id: str, cursor: int = 0, count: int = 20):
        """分页获取作品列表。cursor 为接口 max_cursor（时间戳语义），原样回传。"""
        works = []
        try:
            params = {
                "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
                "sec_user_id": user_id,
                "max_cursor": str(cursor),
                "locate_query": "false", "show_live_replay_strategy": "1",
                "need_time_list": "1", "time_list_query": "0", "whale_cut_token": "",
                "cut_version": "1", "count": str(count),
                "publish_video_strategy_type": "2",
                "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
                "browser_language": "zh-CN", "browser_platform": "Win32",
                "browser_name": "Chrome", "browser_version": WORKS_UA_CHROME_VER,
                "browser_online": "true", "engine_name": "Blink",
                "browser_version_full": WORKS_UA_CHROME_VER,
                "os_name": "Windows", "os_version": "10", "platform": "PC",
            }
            url = "https://www.douyin.com/aweme/v1/web/aweme/post/?" + self._sign_params(params)
            resp = await self.client.get(url, headers=await self._works_headers())

            # 关键：抖音风控拦截时返回的是**纯文本**而非 JSON，原实现直接 resp.json()
            # 会抛 "Expecting value: line 1 column 1 (char 0)"，把真实原因
            # （如 "Blocked by ArgusSecurityPlugin Uifid Not Found"）完全吞掉，
            # 用户只看到一句无法理解的解析错误。这里先按状态码/内容分类。
            if resp.status_code != 200:
                body = (resp.text or "")[:300]
                logger.warning(
                    f"抖音作品接口 HTTP {resp.status_code}: {body[:150]}"
                )
                raise classify_platform_error(
                    f"抖音作品接口 HTTP {resp.status_code}: {body}",
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError:
                # 200 但非 JSON：通常是风控挑战页/空响应
                body = (resp.text or "")[:300]
                logger.warning(f"抖音作品接口返回非 JSON: {body[:150]}")
                raise classify_platform_error(
                    f"抖音作品接口返回非 JSON（可能被风控）: {body}"
                ) from None

            if data.get("status_code") not in (0, None):
                msg = (
                    f"抖音作品列表接口失败: status_code={data.get('status_code')} "
                    f"{(data.get('status_msg') or '')[:120]}"
                )
                raise classify_platform_error(msg)
            for aweme in data.get("aweme_list") or []:
                w = self._aweme_to_work(aweme)
                if w and w.work_id:
                    works.append(w)
            has_more = data.get("has_more") in (1, True, "1")
            try:
                # max_cursor=0 是合法值（不可用 or 吞掉），缺失才回退当前游标
                raw = data.get("max_cursor")
                next_cursor = int(raw) if raw is not None else cursor
            except (ValueError, TypeError):
                next_cursor = cursor
            return works, next_cursor, has_more
        except PlatformError:
            # 已分类（Cookie 失效 / 限流），原样上抛让上层决定告警还是退避
            raise
        except Exception as e:
            logger.error(f"获取抖音作品列表失败 {user_id}: {e}")
            raise classify_platform_error(f"获取抖音作品列表失败: {e}") from e

    @staticmethod
    def _aweme_to_work(aweme: dict) -> Optional[WorkInfo]:
        if not isinstance(aweme, dict):
            return None
        w = WorkInfo()
        w.work_id = str(aweme.get("aweme_id") or "")
        w.title = aweme.get("desc") or ""
        w.publish_ts = int(aweme.get("create_time") or 0)
        w.duration = (aweme.get("duration") or 0) / 1000.0
        images = aweme.get("images")
        if images:
            # 图集作品：逐图取第一个镜像地址
            w.work_type = "images"
            w.duration = 0
            urls = []
            for img in images:
                if isinstance(img, dict):
                    lst = img.get("url_list") or []
                    if lst:
                        urls.append(lst[0])
            w.download_urls = urls
        else:
            video = aweme.get("video") or {}
            play = video.get("play_addr") or video.get("download_addr") or {}
            lst = play.get("url_list") or []
            if lst:
                u = lst[0]
                if isinstance(u, str) and u.startswith("/"):
                    u = "https://www.douyin.com" + u
                w.download_urls = [u]
            cover = (video.get("cover") or {}).get("url_list") or []
            if cover:
                w.cover_url = cover[0]
        return w

    async def find_room_id_by_user(self, user_id: str) -> str:
        """由 sec_uid 解析直播间 web_rid（需登录 Cookie，游客态接口不返回 owner）。

        enter API 支持 sec_user_id 入参：data.data.data[0].web_rid 即直播间号。
        """
        if not user_id:
            return ""
        try:
            params = {
                "aid": "6383", "app_name": "douyin_web", "device_platform": "web",
                "enter_from": "web_live", "cookie_enabled": "true",
                "browser_language": "zh-CN", "browser_platform": "Win32",
                "browser_name": "Chrome", "browser_version": WORKS_UA_CHROME_VER,
                "sec_user_id": user_id,
            }
            url = "https://live.douyin.com/webcast/room/web/enter/?" + self._sign_params(params)
            resp = await self.client.get(url, headers=await self._works_headers())
            data = resp.json()
            if data.get("status_code") == 0:
                rooms = (data.get("data") or {}).get("data") or []
                if rooms and isinstance(rooms[0], dict):
                    return str(rooms[0].get("web_rid") or "")
            return ""
        except Exception as e:
            logger.warning(f"抖音 sec_uid→直播间解析失败 {user_id}: {e}")
            return ""
