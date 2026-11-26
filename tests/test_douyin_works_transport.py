"""回归：抖音作品 / 用户信息请求必须经由平台自带的 httpx 客户端发出。

背景（2026-11-27 根因排查）：这两个接口曾改用 wreq（Rust TLS 指纹客户端）发送，
抖音服务端对 wreq 的请求一律返回 200 空响应体——与 Cookie、签名、出口 IP、边缘节点
都无关；同一条已签名的 URL 改用 httpx 发送即可正常拿到作品（NAS 实测 11 个主播中
9 个返回作品，其余 2 个是同一账号、本身没有可见作品）。

做法：把 wreq 换成「一碰就报错」的替身，并断言请求确实到达 httpx 客户端。
若有人再把请求改回 wreq，这里会失败。

局限：只能防止代码层面的回退，无法感知抖音服务端策略的后续变化。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

# app.config 在导入时会创建输出目录，必须先指到临时目录
_TMP = tempfile.mkdtemp(prefix="live-recorder-test-")
os.environ.setdefault("LIVE_RECORDER_OUTPUT_DIR", _TMP)
os.environ.setdefault("LIVE_RECORDER_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/test.db")

import httpx  # noqa: E402

from app.services.platform.douyin import DouyinPlatform  # noqa: E402

SEC_UID = "MS4wLjABAAAAregression-test-only"
COOKIE = (
    "UIFID=uifid-test-0123456789; {{SECRET_6g2fmr6i}}; "
    "s_v_web_id=verify_test_abc; sessionid=test-session"
)


class _NoWreq:
    """冒充 wreq 模块：任何属性访问都报错。"""

    def __getattr__(self, name):
        raise RuntimeError("抖音作品/用户信息接口不应经由 wreq 发请求（服务端对其返回 200 空体）")


def _ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


class DouyinWorksTransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 外部签名服务路径与本测试无关：强制走内置签名 + 直连
        self.enterContext(mock.patch.object(DouyinPlatform, "_sign_api_url", return_value=""))
        self.enterContext(mock.patch.dict(sys.modules, {"wreq": _NoWreq()}))

    async def _platform_with_mock(self, handler) -> DouyinPlatform:
        platform = DouyinPlatform(cookie=COOKIE)
        await platform.client.aclose()
        platform.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(platform.client.aclose)
        return platform

    async def test_works_request_goes_through_httpx_client(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            return _ok({
                "status_code": 0, "has_more": 0, "max_cursor": 0,
                "aweme_list": [
                    {"aweme_id": "1001", "desc": "作品一", "create_time": 1700000000, "duration": 1000},
                    {"aweme_id": "1002", "desc": "作品二", "create_time": 1700000001, "duration": 2000},
                ],
            })

        platform = await self._platform_with_mock(handler)
        works, cursor, more = await platform.get_user_works(SEC_UID, 0, 20)

        self.assertEqual([w.work_id for w in works], ["1001", "1002"])
        self.assertEqual(seen["url"].split("?")[0], "https://www.douyin.com/aweme/v1/web/aweme/post/")
        query = dict(parse_qsl(urlsplit(seen["url"]).query, keep_blank_values=True))
        # Argus 网关要求的签名三件套 + 业务参数都在查询串里
        self.assertTrue({"sec_user_id", "a_bogus", "uifid", "timestamp", "x-secsdk-web-signature"} <= set(query))
        # 签名在查询串与请求头里是同一个值；登录态 Cookie 随请求发出
        self.assertEqual(seen["headers"]["x-secsdk-web-signature"], query["x-secsdk-web-signature"])
        self.assertIn("sessionid=test-session", seen["headers"]["cookie"])

    async def test_user_info_request_goes_through_httpx_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _ok({"status_code": 0, "user": {
                "nickname": "测试主播",
                "avatar_thumb": {"url_list": ["https://example.com/avatar.jpg"]},
            }})

        platform = await self._platform_with_mock(handler)
        info = await platform.get_user_info(SEC_UID)

        self.assertEqual(info.nickname, "测试主播")
        self.assertEqual(info.avatar_url, "https://example.com/avatar.jpg")


if __name__ == "__main__":
    unittest.main()
