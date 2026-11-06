"""astrbot_plugin_free_search 纯逻辑单测（标准库 unittest，不依赖网络）。

运行：
    cd AstrBot/data/plugins/astrbot_plugin_free_search
    python -m unittest discover -s tests -v
"""

import asyncio
import importlib.util
import ipaddress
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from unittest import mock

import httpx

# 导入 astrbot 时会按 ASTRBOT_ROOT（未设置则用 cwd）创建 data/ 等运行时目录。
# 这里指向临时目录，避免跑测试把产物写进插件目录或真实的 AstrBot 数据目录。
os.environ.setdefault(
    "ASTRBOT_ROOT",
    tempfile.mkdtemp(prefix="astrbot-plugin-test-root-"),
)


def _find_astrbot_root(start: pathlib.Path) -> pathlib.Path | None:
    current = start
    for _ in range(10):
        if (current / "astrbot" / "__init__.py").exists():
            return current
        current = current.parent
    return None


PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = _find_astrbot_root(PLUGIN_DIR)
if ASTRBOT_ROOT is not None and str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_free_search_main",
    PLUGIN_DIR / "main.py",
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def make_plugin(**overrides):
    """构造一个不依赖 AstrBot 运行时的插件实例（配置用普通 dict）。"""
    plugin = mod.FreeSearchPlugin.__new__(mod.FreeSearchPlugin)
    config = {
        "engine": "auto",
        "max_results": 5,
        "snippet_max_chars": 220,
        "max_total_chars": 3500,
        "max_calls_per_minute": 8,
        "max_web_read_per_minute": 5,
        "session_max_chars": 15000,
        "session_budget_idle_minutes": 30,
        "cache_ttl": 300,
        "timeout": 10,
        "web_read_max_chars": 6000,
        "web_read_max_bytes": 2_000_000,
        "paraphrase_hint": True,
        "tavily_include_answer": False,
        "proxy": "",
    }
    config.update(overrides)
    plugin.config = config
    plugin._cache = {}
    plugin._calls = {}
    plugin._session_chars = {}
    plugin._last_cleanup = 0.0
    return plugin


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"", charset_encoding=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.charset_encoding = charset_encoding

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    async def aiter_bytes(self):
        yield self._body


class FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def stream(self, method, url, follow_redirects=False):
        self.urls.append(url)
        return FakeStream(self._responses.pop(0))


class TestSanitise(unittest.TestCase):
    def test_removes_replacement_and_control_chars(self):
        self.assertEqual(mod.FreeSearchPlugin._sanitise("ab\ufffdcd\x00\x07ef"), "abcdef")

    def test_merges_surrogate_pair(self):
        emoji = chr(0xD83D) + chr(0xDE00)
        self.assertEqual(mod.FreeSearchPlugin._sanitise(f"a{emoji}b"), "a😀b")

    def test_drops_lone_surrogate(self):
        self.assertEqual(mod.FreeSearchPlugin._sanitise("a" + chr(0xD83D) + "b"), "ab")

    def test_collapses_whitespace(self):
        self.assertEqual(mod.FreeSearchPlugin._sanitise(" a \n\t b "), "a b")


class TestPeelDate(unittest.TestCase):
    def test_chinese_date(self):
        date, body = mod.FreeSearchPlugin._peel_date("2025年2月28日 · 这是正文")
        self.assertEqual(date, "2025年2月28日")
        self.assertEqual(body, "这是正文")

    def test_relative_date(self):
        date, body = mod.FreeSearchPlugin._peel_date("4天前 - 一些内容")
        self.assertEqual(date, "4天前")
        self.assertEqual(body, "一些内容")

    def test_no_date(self):
        date, body = mod.FreeSearchPlugin._peel_date("普通文本")
        self.assertEqual(date, "")
        self.assertEqual(body, "普通文本")


class TestDenoise(unittest.TestCase):
    def test_drops_long_run_of_short_lines(self):
        noise = "\n".join(["首页", "登录", "注册", "搜索", "更多", "下一页"])
        text = noise + "\n这是一段足够长的正文内容，描述了某件事的来龙去脉，并以句号结尾。"
        cleaned = mod.FreeSearchPlugin._denoise(text)
        self.assertNotIn("首页", cleaned)
        self.assertIn("正文内容", cleaned)

    def test_keeps_short_run_of_four(self):
        text = "要点一\n要点二\n要点三\n要点四\n这是一段足够长的正文内容，描述了某件事，并以句号结尾。"
        cleaned = mod.FreeSearchPlugin._denoise(text)
        self.assertIn("要点一", cleaned)


class TestClip(unittest.TestCase):
    def test_clip(self):
        self.assertEqual(mod.FreeSearchPlugin._clip("abcdef", 3), "abc…")
        self.assertEqual(mod.FreeSearchPlugin._clip("abc", 10), "abc")
        self.assertEqual(mod.FreeSearchPlugin._clip("abcdef", 0), "abcdef")


class TestIpChecks(unittest.TestCase):
    def test_local_addresses(self):
        for host in (
            "127.0.0.1",
            "10.1.2.3",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.1.1",
            "localhost",
            "foo.internal",
            "::1",
        ):
            self.assertTrue(mod._is_local_address(host), host)

    def test_public_addresses(self):
        for host in ("8.8.8.8", "1.1.1.1", "example.com"):
            self.assertFalse(mod._is_local_address(host), host)

    def test_ipv4_mapped_ipv6_is_private(self):
        self.assertFalse(mod._ip_is_public(ipaddress.ip_address("::ffff:127.0.0.1")))
        self.assertTrue(mod._ip_is_public(ipaddress.ip_address("8.8.8.8")))


class TestHostAllowed(unittest.IsolatedAsyncioTestCase):
    async def test_literal_private_rejected(self):
        plugin = make_plugin()
        self.assertFalse(await plugin._host_allowed("127.0.0.1"))

    async def test_literal_public_allowed(self):
        plugin = make_plugin()
        self.assertTrue(await plugin._host_allowed("8.8.8.8"))

    async def test_localhost_name_rejected(self):
        plugin = make_plugin()
        self.assertFalse(await plugin._host_allowed("localhost"))

    async def test_domain_resolving_to_private_rejected(self):
        plugin = make_plugin()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with mock.patch.object(mod.socket, "getaddrinfo", return_value=infos):
            self.assertFalse(await plugin._host_allowed("evil.example.com"))

    async def test_domain_resolving_to_public_allowed(self):
        plugin = make_plugin()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        with mock.patch.object(mod.socket, "getaddrinfo", return_value=infos):
            self.assertTrue(await plugin._host_allowed("example.com"))


class TestDecodeBody(unittest.TestCase):
    def test_utf8(self):
        raw = "你好世界".encode("utf-8")
        self.assertEqual(mod._decode_body(raw, FakeResponse()), "你好世界")

    def test_gbk_declared_by_meta(self):
        raw = '<meta charset="gbk">中文测试'.encode("gbk")
        self.assertIn("中文测试", mod._decode_body(raw, FakeResponse()))

    def test_gbk_without_declaration(self):
        raw = "中文测试内容".encode("gbk")
        self.assertEqual(mod._decode_body(raw, FakeResponse()), "中文测试内容")


class TestExtractText(unittest.TestCase):
    def test_prefers_article_and_drops_nav_footer(self):
        body = "这是正文内容，用来测试正文提取是否只保留文章主体。" * 12
        html = (
            "<html><body>"
            "<nav>菜单 首页 关于 联系</nav>"
            f"<article><h1>标题</h1><p>{body}</p></article>"
            "<footer>版权所有</footer>"
            "</body></html>"
        )
        text = mod._extract_text(html)
        self.assertIn("这是正文内容", text)
        self.assertNotIn("菜单", text)
        self.assertNotIn("版权所有", text)

    def test_strip_html(self):
        self.assertEqual(mod._strip_html("<p>Hello <b>world</b></p>"), "Hello world")


class TestDedupe(unittest.TestCase):
    def test_dedupe_by_url(self):
        results = [
            {"title": "a", "url": "https://x.com/1", "snippet": ""},
            {"title": "b", "url": "https://x.com/1#frag", "snippet": ""},
            {"title": "c", "url": "https://x.com/2", "snippet": ""},
        ]
        deduped = mod.FreeSearchPlugin._dedupe_results(results)
        self.assertEqual([r["title"] for r in deduped], ["a", "c"])


class TestUnwrapDdg(unittest.TestCase):
    def test_unwrap(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=1"
        self.assertEqual(mod.FreeSearchPlugin._unwrap_ddg(href), "https://example.com/a")

    def test_plain(self):
        self.assertEqual(
            mod.FreeSearchPlugin._unwrap_ddg("https://example.com/b"),
            "https://example.com/b",
        )


class TestGuard(unittest.TestCase):
    def test_rate_limit(self):
        plugin = make_plugin(max_calls_per_minute=2)
        self.assertIsNone(plugin._guard("s"))
        self.assertIsNone(plugin._guard("s"))
        self.assertIsNotNone(plugin._guard("s"))

    def test_rate_limit_disabled(self):
        plugin = make_plugin(max_calls_per_minute=0)
        for _ in range(20):
            self.assertIsNone(plugin._guard("s"))

    def test_session_budget(self):
        plugin = make_plugin(session_max_chars=100, max_calls_per_minute=0)
        plugin._add_session_chars("s", 100)
        self.assertIsNotNone(plugin._guard("s"))

    def test_session_budget_idle_reset(self):
        plugin = make_plugin(session_max_chars=100, max_calls_per_minute=0)
        plugin._session_chars["s"] = (100, 0.0)
        self.assertIsNone(plugin._guard("s"))

    def test_separate_rate_keys(self):
        plugin = make_plugin(max_calls_per_minute=1, max_web_read_per_minute=1)
        self.assertIsNone(plugin._guard("s", rate_key="s"))
        self.assertIsNotNone(plugin._guard("s", rate_key="s"))
        self.assertIsNone(plugin._guard("s", rate_key="s#read"))


class TestFormatResults(unittest.TestCase):
    def test_total_limit_truncates(self):
        plugin = make_plugin(max_total_chars=60, snippet_max_chars=220)
        results = [
            {"title": f"标题{i}", "url": f"https://x.com/{i}", "snippet": "内容" * 50}
            for i in range(5)
        ]
        text = plugin._format_results("测试", results)
        self.assertIn("省略", text)
        self.assertLess(len(text), 300)

    def test_includes_date_and_snippet(self):
        plugin = make_plugin()
        results = [
            {
                "title": "标题",
                "url": "https://x.com/1",
                "snippet": "2025年2月28日 · 这是正文内容",
            }
        ]
        text = plugin._format_results("测试", results)
        self.assertIn("（2025年2月28日）", text)
        self.assertIn("摘要: 这是正文内容", text)


class TestFetchPageSecurity(unittest.IsolatedAsyncioTestCase):
    async def test_direct_private_blocked(self):
        plugin = make_plugin()
        client = FakeClient([])
        ok, payload = await plugin._fetch_page(client, "http://127.0.0.1:6185/", 1024)
        self.assertFalse(ok)
        self.assertIn("内网", payload)
        self.assertEqual(client.urls, [])

    async def test_redirect_to_private_blocked(self):
        plugin = make_plugin()
        client = FakeClient([FakeResponse(302, {"location": "http://127.0.0.1:6185/"})])

        async def fake_allowed(host):
            return host not in ("127.0.0.1", "localhost")

        plugin._host_allowed = fake_allowed
        ok, payload = await plugin._fetch_page(client, "https://example.com/", 1024)
        self.assertFalse(ok)
        self.assertIn("内网", payload)

    async def test_public_page_ok(self):
        plugin = make_plugin()
        html = "<html><article><p>Hello 正文</p></article></html>".encode("utf-8")
        client = FakeClient([FakeResponse(200, {"content-type": "text/html"}, html)])

        async def fake_allowed(host):
            return True

        plugin._host_allowed = fake_allowed
        ok, payload = await plugin._fetch_page(client, "https://example.com/", 1024)
        self.assertTrue(ok)
        text, truncated = payload
        self.assertIn("Hello 正文", text)
        self.assertFalse(truncated)

    async def test_size_limit_truncates(self):
        plugin = make_plugin()
        html = ("<p>" + "A" * 5000 + "</p>").encode("utf-8")
        client = FakeClient([FakeResponse(200, {"content-type": "text/html"}, html)])

        async def fake_allowed(host):
            return True

        plugin._host_allowed = fake_allowed
        ok, payload = await plugin._fetch_page(client, "https://example.com/", 100)
        self.assertTrue(ok)
        _text, truncated = payload
        self.assertTrue(truncated)

    async def test_too_many_redirects(self):
        plugin = make_plugin()
        responses = [
            FakeResponse(302, {"location": f"https://example.com/{i}"})
            for i in range(5)
        ]
        client = FakeClient(responses)

        async def fake_allowed(host):
            return True

        plugin._host_allowed = fake_allowed
        ok, payload = await plugin._fetch_page(client, "https://example.com/0", 1024)
        self.assertFalse(ok)
        self.assertIn("重定向次数过多", payload)


if __name__ == "__main__":
    unittest.main()
