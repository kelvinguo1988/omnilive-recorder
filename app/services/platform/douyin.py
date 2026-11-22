"""抖音直播适配器"""
import re
import json
import random
import logging
from typing import Optional
from app.config import settings
from app.services.platform.base import (
    BasePlatform, RoomInfo, PlatformFactory, UserInfo, WorkInfo,
    WORKS_UA, WORKS_UA_CHROME_VER, PlatformError, classify_platform_error,
)
from app.services.platform.vendor.abogus import ABogus, BrowserFingerprintGenerator
from app.services.platform.vendor.websign import sign as websign_sign

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

    def _cookie_field_ci(self, *names: str) -> str:
        """按大小写不敏感方式取 Cookie 字段（SDK 接受 uifid/UIFID/uifid_temp 等拼写）"""
        cookie = self.cookie or ""
        wanted = {n.lower() for n in names}
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k.lower() in wanted and v:
                return v
        return ""

    def _websign(self, signed_qs: str) -> tuple[str, dict]:
        """在 a_bogus 查询串上叠加 Argus 网关的三件套签名。

        Douyin 2026-11 起对作品/用户接口启用 ArgusSecurityPlugin：请求必须携带
        uifid + x-secsdk-web-signature + x-secsdk-web-expire（query 与 headers 双份），
        缺任一即 403 "Uifid/Signature Not Found"。算法为
        md5(f"{uifid}_{timestamp}_{SALT}_{query}")，纯 Python 可实现
        （移植自 Douyin_TikTok_Download_API v5.1.11 websign.py，Apache-2.0）。
        uifid 只能来自登录态 Cookie（游客态原生铸造不支持抖音）。
        """
        uifid = self._cookie_field_ci("uifid", "uifid_temp", "uifidtemp")
        if not uifid:
            raise CookieInvalidError(
                "Cookie 中缺少 UIFID 设备指纹（Argus 网关必需）。"
                "请从已登录 douyin.com 的浏览器完整复制 Cookie（含 UIFID 字段）"
            )
        pairs = []
        for part in signed_qs.split("&"):
            k, _, v = part.partition("=")
            pairs.append((k, v))
        # 站点真实顺序（DTK 实测逐字节复刻）：
        # ...&a_bogus=..&verifyFp=..&fp=..&uifid=..&timestamp=..&x-secsdk-web-signature=..
        # verifyFp 与 fp 都是 s_v_web_id 的值，有则成对追加
        s_v_web_id = self._cookie_field_ci("s_v_web_id")
        if s_v_web_id:
            pairs.append(("verifyFp", s_v_web_id))
            pairs.append(("fp", s_v_web_id))
        query, signature, headers = websign_sign(pairs, uifid)
        return query, headers

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

    def _cookie_field(self, name: str) -> str:
        """从 Cookie 串里取指定字段的值（如 UIFID/UIFID_TEMP/msToken）"""
        cookie = self.cookie or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name and v:
                return v
        return ""

    def _sign_params(self, params: dict) -> str:
        """对查询串做 a_bogus 签名，返回已附加 a_bogus 的完整参数串

        P1-1: 复用进程级签名器（见模块顶部 _get_abogus_signer），指纹只在首次构造时
        生成一次。原实现每次调用都 ABogus(user_agent=WORKS_UA) 新建实例，
        构造函数里的 random.randint 让屏宽/高/outer 尺寸逐请求漂移，
        同一会话的指纹前后不一致 —— 这是抖音 -352/空列表的高发诱因。

        P2-U: Cookie 里有 UIFID 时必须同步注入 query 的 uifid 参数 ——
        2026-11 起作品/用户接口被 ArgusSecurityPlugin 网关拦截，缺 uifid
        直接 403 "Blocked by ArgusSecurityPlugin Uifid Not Found"（游客态
        ttwid 不含 UIFID，此参数只能来自登录态 Cookie）。
        """
        # uifid 不在此注入：站点顺序里 uifid 位于 a_bogus 之后，由 _websign 层
        # 按 Argus 要求的位置追加（放错位置会让 Argus 报 Sign Invalid）
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return _get_abogus_signer(WORKS_UA).generate_abogus(qs)[0]

    # ---------- 外部签名服务（Douyin_TikTok_Download_API 等） ----------

    def _sign_api_url(self) -> str:
        """外部签名服务基地址（空=未启用内置 a_bogus 直连路径）"""
        return (getattr(settings, "douyin_sign_api_url", "") or "").rstrip("/")

    async def _sign_api_get(self, path: str, params: dict) -> dict:
        """调用外部签名服务的通用 GET：服务端内部完成 uifid/签名/TLS 对抗。

        经典契约（V4.x，NAS 部署最广）：响应 {code:200, data:{...}}；
        服务端失败返回 HTTP 400 + detail。非 200/code!=200 统一按平台错误分类，
        让上层 Cookie 熔断/退避/UI 日志机制照常工作。
        """
        url = f"{self._sign_api_url()}{path}"
        resp = await self.client.get(url, params=params)
        if resp.status_code != 200:
            detail = resp.text[:300]
            raise classify_platform_error(
                f"抖音签名服务 HTTP {resp.status_code}: {detail}",
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError:
            raise classify_platform_error(
                f"抖音签名服务返回非 JSON: {(resp.text or '')[:200]}"
            ) from None
        if payload.get("code") not in (200, 0, None):
            raise classify_platform_error(
                f"抖音签名服务业务失败: code={payload.get('code')} "
                f"{str(payload.get('detail') or payload.get('message') or '')[:200]}"
            )
        return payload.get("data") or {}

    async def _fetch_user_works_via_api(self, user_id: str, cursor: int, count: int):
        """经外部签名服务拉取作品列表（响应结构与抖音 Web API 一致，复用解析）"""
        data = await self._sign_api_get("/douyin/web/fetch_user_post_videos", {
            "sec_user_id": user_id,
            "max_cursor": str(cursor),
            "count": str(count),
        })
        works = []
        for aweme in data.get("aweme_list") or []:
            w = self._aweme_to_work(aweme)
            if w and w.work_id:
                works.append(w)
        has_more = data.get("has_more") in (1, True, "1")
        try:
            raw = data.get("max_cursor")
            next_cursor = int(raw) if raw is not None else cursor
        except (ValueError, TypeError):
            next_cursor = cursor
        return works, next_cursor, has_more

    async def get_user_info(self, user_id: str) -> UserInfo:
        info = UserInfo(user_id=user_id)
        if not user_id:
            return info
        if self._sign_api_url():
            # 外部签名服务路径：服务端自行对抗 Argus（需在服务内配置登录身份）
            try:
                data = await self._sign_api_get("/douyin/web/handler_user_profile", {
                    "sec_user_id": user_id,
                })
                user = (data.get("user") or {}) if isinstance(data, dict) else {}
                info.nickname = user.get("nickname") or ""
                thumb = (user.get("avatar_thumb") or {}).get("url_list") or []
                if thumb:
                    info.avatar_url = thumb[0]
            except Exception as e:
                logger.warning(f"签名服务获取抖音用户信息失败 {user_id}: {e}")
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
            signed = self._sign_params(params)
            query, ws_headers = self._websign(signed)
            url = "https://www.douyin.com/aweme/v1/web/user/profile/other/?" + query
            wreq_client = self._works_wreq_client()
            works_headers = {
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                **ws_headers,
            }
            if wreq_client is not None:
                resp = await wreq_client.get(url, headers=works_headers)
                status, text = resp.status, await resp.text()
                if status != 200:
                    raise classify_platform_error(
                        f"抖音用户信息接口 HTTP {status}: {text}", status_code=status
                    )
                data = json.loads(text)
            else:
                resp = await self.client.get(
                    url, headers={**await self._works_headers(), **works_headers}
                )
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

    # Argus 假 msToken 兜底参数（DTK tokens.py 语义：126 字符 + "=="，字母表无 j/G 陷阱）
    _MS_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"

    def _ms_token(self) -> str:
        """msToken：Cookie 真值优先（登录态 jar 里就有），缺失时本地伪造兜底。

        DTK 实测（2026-09-09，aweme/detail）：真值/伪造/缺失三种形态都返回完整
        payload——douyin 在该端点不校验 msToken 质量，伪造只为形似。
        """
        real = self._cookie_field_ci("msToken")
        if real:
            return real
        rng = random.Random()
        return "".join(rng.choice(self._MS_ALPHABET) for _ in range(126)) + "=="

    def _works_wreq_client(self):
        """作品接口专用 wreq 客户端（Chrome131 TLS 指纹 + Cookie jar）。

        Argus 网关除签名外还校验 TLS 指纹——httpx 的 Python TLS 会被
        静默拒绝（200 空响应体），必须用浏览器指纹的 TLS 栈。wreq 是
        reqwest 的 Python 绑定（DTK 同款传输层），缺失时返回 None 回退 httpx
        （此时抖音作品会拿到空响应体并在日志中如实呈现）。
        """
        if not getattr(self, "_wreq_client", None):
            try:
                import wreq
            except ImportError:
                logger.warning(
                    "wreq 未安装（pip install wreq），抖音作品请求将使用 httpx 直连——"
                    "Argus 网关会以 200 空响应体静默拒绝，作品列表将为空"
                )
                return None
            jar = wreq.Jar()
            for part in (self.cookie or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k and v:
                    try:
                        jar.add(wreq.Cookie(name=k, value=v, domain="douyin.com", path="/"),
                                "https://www.douyin.com")
                    except Exception:
                        pass
            self._wreq_jar = jar
            self._wreq_client = wreq.Client(emulation=wreq.Emulation.Chrome131, jar=jar)
        return self._wreq_client

    async def get_user_works(self, user_id: str, cursor: int = 0, count: int = 20):
        """分页获取作品列表。cursor 为接口 max_cursor（时间戳语义），原样回传。"""
        # 外部签名服务路径：Argus 网关（uifid+签名+TLS 指纹）下内置 a_bogus 直连已失效，
        # 配置了签名服务就走它，由服务端完成对抗
        if self._sign_api_url():
            return await self._fetch_user_works_via_api(user_id, cursor, count)
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
            # msToken：Cookie 真值优先，否则伪造（douyin 在该端点不校验 msToken 质量）
            params["msToken"] = self._ms_token()
            signed = self._sign_params(params)
            query, ws_headers = self._websign(signed)
            url = "https://www.douyin.com/aweme/v1/web/aweme/post/?" + query

            # ttwid 缺失时先补（wreq jar 需要；Cookie 已含则跳过）
            wreq_client = self._works_wreq_client()
            if wreq_client is not None and not self._cookie_field_ci("ttwid"):
                ttwid = await self._get_ttwid()
                if ttwid:
                    try:
                        self._wreq_jar.add(
                            wreq.Cookie(name="ttwid", value=ttwid, domain="douyin.com", path="/"),
                            "https://www.douyin.com",
                        )
                    except Exception:
                        pass

            works_headers = {
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                **ws_headers,
            }
            if wreq_client is not None:
                # wreq: Chrome131 TLS 指纹（Argus 会静默拒绝 Python TLS——200 空响应体）
                resp = await wreq_client.get(url, headers=works_headers)
                status, text = resp.status, await resp.text()
                if status != 200:
                    logger.warning(f"抖音作品接口 HTTP {status}: {text[:150]}")
                    raise classify_platform_error(
                        f"抖音作品接口 HTTP {status}: {text}", status_code=status
                    )
                if not text:
                    raise classify_platform_error(
                        "抖音作品接口返回空响应体（游客身份的空信封或风控软拒绝："
                        "作品订阅需要含 UIFID+sessionid 的登录态 Cookie）"
                    )
                data = json.loads(text)
            else:
                resp = await self.client.get(
                    url, headers={**await self._works_headers(), **works_headers}
                )
                status, text = resp.status_code, resp.text
                if status != 200:
                    logger.warning(f"抖音作品接口 HTTP {status}: {(text or '')[:150]}")
                    raise classify_platform_error(
                        f"抖音作品接口 HTTP {status}: {text}", status_code=status
                    )
                data = resp.json()

            # 200 但非 JSON / 空响应体：Argus 网关的软拒绝形态（签名/TLS 通过但
            # 身份层被拦），如实分类上抛
            if not text:
                raise classify_platform_error(
                    "抖音作品接口返回空响应体（Argus 风控软拒绝："
                    "请确认已在「系统设置」填写含 UIFID 的登录态 Cookie）"
                )
            try:
                data = json.loads(text)
            except ValueError:
                # 200 但非 JSON：通常是风控挑战页/空响应
                body = (text or "")[:300]
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
