"""AstrBot 免费联网搜索插件

给 LLM 注册 web_search 工具，让 AI 在聊天时自动联网搜索（无需任何 API Key）：
- Bing 网页搜索：默认主力引擎，国内可直连
- DuckDuckGo HTML：备用引擎（需要网络可达 ddg，国内裸连不通，留给有代理的环境）
- Tavily API：可选，质量最好

AI 拿到搜索结果后会以自然口语转述，而不是机械罗列链接。
另提供 /搜索 关键词 指令手动触发，同样走 LLM 转述。

v1.3.0 改进：
- web_read 增加重定向逐跳校验 + DNS 解析校验，修复 SSRF（含重定向绕过）
- web_read 纳入会话级频率/字数闸门，并复用正文降噪
- 网页下载改为流式 + 大小上限 + 编码兜底（GBK/GB18030）
- 会话字数预算支持空闲重置，避免长期运行后永久锁死
- 缓存 key 纳入引擎/参数；搜索结果按 URL 去重
- 支持 proxy；web_search 支持 site / time_range / max_results
- Tavily 支持 include_answer
"""

import asyncio
import html as html_mod
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_PARAPHRASE_HINT = (
    "\n\n（以上是实时搜索结果，请用自然的中文口语向用户转述要点，"
    "不要逐条罗列、不要机械地复述标题链接；用户追问出处时再给出对应链接。）"
)

_EMPTY_SEARCH_HINT = (
    "（本次联网搜索没有返回任何结果，可能是网络不可达或所有搜索引擎都失败了。"
    "请如实告诉用户暂时查不到，不要编造内容，可以建议换个关键词或稍后再试。）"
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".corp",
    ".localhost",
    ".localdomain",
)
_MAX_REDIRECTS = 3
_TIME_RANGES = ("day", "week", "month", "year")
_BING_FRESHNESS = {"day": "ez1", "week": "ez2", "month": "ez3", "year": "ez5"}
_DDG_TIME = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _cfg_int(cfg, key: str, default: int, minimum: int | None = None) -> int:
    """从配置里安全读取整数，坏值回退默认值。"""
    try:
        value = int(cfg.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """判断一个 IP 是否为可公网访问的地址（用于阻断 SSRF）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


def _is_local_address(host: str) -> bool:
    """同步版粗略判断：只看字面 IP 与明显的内网主机名（不做 DNS 解析）。

    真正的拦截走 ``_host_allowed``（含 DNS 解析与逐跳重定向校验）。
    """
    host = (host or "").strip().strip(".").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not _ip_is_public(ip)


class _TextExtractor(HTMLParser):
    """从 HTML 中提取纯文本。

    与旧版相比：
    - 跳过 script/style/noscript/svg 等不可见标签；
    - 跳过 nav/footer/aside 这类导航噪声；
    - 在块级标签处断行，方便后续 ``_denoise`` 按行判断噪声；
    - 支持 ``main_only``：只取 ``<article>`` / ``<main>`` 内的正文。
    """

    _SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
        "template",
        "head",
        "title",
    }
    _BOILER_TAGS = {"nav", "footer", "aside"}
    _MAIN_TAGS = {"article", "main"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "li",
        "br",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "blockquote",
        "pre",
        "td",
        "th",
        "dt",
        "dd",
        "ul",
        "ol",
        "table",
        "figcaption",
        "caption",
    }

    def __init__(self, main_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        self._boiler_depth = 0
        self._main_depth = 0
        self._main_only = main_only

    def _flush(self) -> None:
        if self._buf:
            text = " ".join(part for part in self._buf if part).strip()
            if text:
                self._parts.append(text)
            self._buf = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self._BOILER_TAGS:
            self._boiler_depth += 1
        if tag in self._MAIN_TAGS:
            self._main_depth += 1
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._flush()
        if tag in self._MAIN_TAGS and self._main_depth > 0:
            self._main_depth -= 1
        if tag in self._BOILER_TAGS and self._boiler_depth > 0:
            self._boiler_depth -= 1

    def handle_data(self, data):
        if self._skip_depth or self._boiler_depth:
            return
        if self._main_only and self._main_depth == 0:
            return
        data = data.strip()
        if data:
            self._buf.append(data)

    def text(self) -> str:
        self._flush()
        return "\n".join(self._parts)


def _parse_text(html_text: str, main_only: bool = False) -> str:
    parser = _TextExtractor(main_only=main_only)
    try:
        parser.feed(html_text or "")
        parser.close()
        return parser.text()
    except Exception:
        return ""


def _extract_text(html_text: str) -> str:
    """提取网页正文：有 article/main 时优先只取正文，否则取整页（已去导航）。"""
    if not html_text:
        return ""
    has_main = bool(re.search(r"<(?:article|main)[\s>]", html_text, re.I))
    if has_main:
        text = _parse_text(html_text, main_only=True)
        if len(text) >= 200:
            return text
    text = _parse_text(html_text, main_only=False)
    if text:
        return text
    return re.sub(r"<[^>]+>", " ", html_text)


def _strip_html(fragment: str) -> str:
    text = _parse_text(fragment or "", main_only=False)
    if not text:
        text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", text).strip()


def _decode_body(raw: bytes, resp: httpx.Response) -> str:
    """解码网页字节：优先响应头/meta 声明的编码，再 utf-8，最后 gb18030。"""
    candidates: list[str] = []
    encoding = getattr(resp, "charset_encoding", None)
    if encoding:
        candidates.append(encoding)
    match = re.search(
        rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)",
        raw[:4096],
        re.I,
    )
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    seen: set[str] = set()
    for enc in candidates:
        key = enc.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 没有可靠声明：先按 utf-8 宽容解码，坏字符多再试 GB18030（中文老站常见）
    text = raw.decode("utf-8", "replace")
    if text.count("\ufffd") <= max(2, len(text) // 1000):
        return text
    try:
        return raw.decode("gb18030")
    except Exception:
        return text


@register(
    name="免费联网搜索",
    author="26875",
    desc="免费联网搜索：web_search 搜索 + web_read 读网页；默认 Bing/DuckDuckGo 零密钥，可选配 Tavily API；AI 自动调用并口语化转述，/搜索 关键词 手动触发",
    version="1.3.0",
)
class FreeSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache: dict[str, tuple[float, list[dict], str]] = {}
        # 会话级防护：调用时间戳 / 累计注入上下文的字符数
        self._calls: dict[str, list[float]] = {}
        self._session_chars: dict[str, tuple[int, float]] = {}
        self._last_cleanup = 0.0

    # ---------------- 引擎实现 ----------------

    def _proxy(self) -> str | None:
        return str(self.config.get("proxy", "") or "").strip() or None

    async def _fetch(self, url: str, params: dict | None = None) -> str:
        timeout = max(3, _cfg_int(self.config, "timeout", 10))
        headers = {
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            proxy=self._proxy(),
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.text

    async def _search_bing(
        self, query: str, max_results: int, time_range: str = ""
    ) -> tuple[list[dict], str]:
        market = str(self.config.get("bing_market", "zh-CN"))
        params = {
            "q": query,
            "count": str(max(10, max_results + 5)),
            "mkt": market,
            "setlang": "zh-hans",
        }
        fresh = _BING_FRESHNESS.get(time_range)
        if fresh:
            params["filters"] = f'ex1:"{fresh}"'
        page = await self._fetch("https://www.bing.com/search", params=params)
        results: list[dict] = []
        for chunk in re.split(r'<li[^>]*class="[^"]*b_algo[^"]*"', page)[1:]:
            if len(results) >= max_results:
                break
            m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
            if not m:
                continue
            url = html_mod.unescape(m.group(1))
            title = _strip_html(m.group(2))
            if not title or url.startswith("javascript:"):
                continue
            sm = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
            snippet = _strip_html(sm.group(1)) if sm else ""
            results.append({"title": title, "url": url, "snippet": snippet})
        return results, ""

    async def _search_ddg(
        self, query: str, max_results: int, time_range: str = ""
    ) -> tuple[list[dict], str]:
        params = {"q": query, "kl": "cn-zh"}
        df = _DDG_TIME.get(time_range)
        if df:
            params["df"] = df
        page = await self._fetch("https://html.duckduckgo.com/html/", params=params)
        results: list[dict] = []
        chunks = re.split(r'<a[^>]+class="result__a"', page)[1:]
        for chunk in chunks:
            if len(results) >= max_results:
                break
            m = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
            if not m:
                continue
            url = self._unwrap_ddg(html_mod.unescape(m.group(1)))
            title = _strip_html(m.group(2))
            if not title:
                continue
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.S)
            snippet = _strip_html(sm.group(1)) if sm else ""
            results.append({"title": title, "url": url, "snippet": snippet})
        return results, ""

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        # DDG 的结果链接是 //duckduckgo.com/l/?uddg=<编码后真实链接>&rut=... 形式
        if "uddg=" in href:
            if href.startswith("//"):
                href = "https:" + href
            qs = parse_qs(urlparse(href).query)
            real = qs.get("uddg", [""])[0]
            if real:
                return unquote(real)
        return href

    async def _search_tavily(
        self, query: str, max_results: int, time_range: str = ""
    ) -> tuple[list[dict], str]:
        api_key = str(self.config.get("tavily_api_key", "")).strip()
        if not api_key:
            raise ValueError(
                "未配置 tavily_api_key，请在插件配置中填入 Tavily 密钥（app.tavily.com 免费注册）"
            )
        timeout = max(3, _cfg_int(self.config, "timeout", 10))
        depth = str(self.config.get("tavily_depth", "basic")).strip().lower()
        if depth not in ("basic", "advanced"):
            depth = "basic"
        body: dict = {
            "query": query,
            "max_results": max_results,
            "search_depth": depth,
        }
        if time_range in _TIME_RANGES:
            body["time_range"] = time_range
        want_answer = bool(self.config.get("tavily_include_answer", False))
        if want_answer:
            body["include_answer"] = True
        async with httpx.AsyncClient(timeout=timeout, proxy=self._proxy()) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        results: list[dict] = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "snippet": str(item.get("content") or "").strip(),
                }
            )
        answer = str(data.get("answer") or "").strip() if want_answer else ""
        return results[:max_results], answer

    # ---------------- 网页读取（SSRF 防护 + 限流 + 编码兜底） ----------------

    async def _host_allowed(self, host: str) -> bool:
        """校验主机是否允许访问：字面 IP 直接判定，域名则解析后校验全部 IP。"""
        host = (host or "").strip().strip(".").lower()
        if not host:
            return False
        if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            return _ip_is_public(ip)
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, socket.getaddrinfo, host, None
                ),
                timeout=5,
            )
        except Exception:
            return False
        if not infos:
            return False
        for info in infos:
            addr = info[4][0]
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                return False
            if not _ip_is_public(resolved):
                return False
        return True

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_bytes: int,
    ) -> tuple[bool, object]:
        """手动逐跳跟随重定向，每一跳都重新校验目标主机。

        Returns:
            (True, (html_text, truncated)) 或 (False, 错误提示)。
        """
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in _ALLOWED_SCHEMES:
                return False, "（读取失败：只支持 http:// 或 https:// 开头的网页链接）"
            if not await self._host_allowed(parsed.hostname or ""):
                return False, "（读取失败：不允许访问本地或内网地址）"
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return False, "（网页打开失败：重定向缺少目标地址）"
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and not (
                    ctype.startswith("text/")
                    or "html" in ctype
                    or "xml" in ctype
                    or "json" in ctype
                ):
                    return False, f"（该链接不是网页（{ctype}），无法提取正文）"
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        truncated = True
                        break
                raw = b"".join(chunks)
                if truncated:
                    raw = raw[:max_bytes]
                return True, (_decode_body(raw, resp), truncated)
        return False, "（网页打开失败：重定向次数过多）"

    async def _read_page(self, url: str) -> tuple[bool, str]:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return False, "（读取失败：只支持 http:// 或 https:// 开头的网页链接）"
        timeout = max(3, _cfg_int(self.config, "timeout", 10))
        max_chars = _cfg_int(self.config, "web_read_max_chars", 6000, 0)
        max_bytes = _cfg_int(self.config, "web_read_max_bytes", 2_000_000, 64 * 1024)
        headers = {
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                proxy=self._proxy(),
            ) as client:
                ok, payload = await self._fetch_page(client, url, max_bytes)
        except Exception as e:
            return False, f"（网页打开失败：{e}。请如实告知用户这个链接暂时读不了，不要编造内容。）"
        if not ok:
            return False, str(payload)
        html_text, truncated = payload
        text = self._sanitise(self._denoise(_extract_text(html_text)))
        if not text:
            return False, "（该页面没有提取到正文，可能是需要浏览器渲染的动态页面）"
        if 0 < max_chars < len(text):
            text = text[:max_chars].rstrip() + "…（内容过长，已截断）"
        if truncated:
            text += "\n（注：页面体积过大，只读取了前一部分内容）"
        return True, text

    # ---------------- 搜索入口（缓存 + 引擎回退 + 去重） ----------------

    def _engine_chain(self) -> list[str]:
        engine = str(self.config.get("engine", "auto")).lower()
        has_tavily = bool(str(self.config.get("tavily_api_key", "")).strip())
        if engine == "tavily":
            # 密钥缺失/调用失败时 tavily 会抛错，自动落到后面的抓取引擎
            return ["tavily", "bing", "ddg"]
        if engine == "bing":
            return ["bing"]
        if engine == "ddg":
            return ["ddg"]
        # auto：配了 tavily 密钥就优先走 API，失败再回退网页抓取
        if has_tavily:
            return ["tavily", "bing", "ddg"]
        return ["bing", "ddg"]

    @staticmethod
    def _normalise_url(url: str) -> str:
        return (url or "").strip().split("#", 1)[0]

    @classmethod
    def _dedupe_results(cls, results: list[dict]) -> list[dict]:
        seen: set[str] = set()
        output: list[dict] = []
        for item in results:
            key = cls._normalise_url(item.get("url", ""))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            output.append(item)
        return output

    async def _search(
        self,
        query: str,
        *,
        site: str = "",
        time_range: str = "",
        max_results: int = 0,
    ) -> tuple[list[dict], str]:
        query = (query or "").strip()[:100]
        if not query:
            return [], ""
        site = (site or "").strip()
        full_query = f"{query} site:{site}" if site else query
        if isinstance(max_results, int) and max_results > 0:
            mr = max_results
        else:
            mr = _cfg_int(self.config, "max_results", 5)
        mr = min(max(1, mr), 10)
        tr = time_range if time_range in _TIME_RANGES else ""
        chain = self._engine_chain()
        depth = str(self.config.get("tavily_depth", "basic")).strip().lower()
        ttl = _cfg_int(self.config, "cache_ttl", 300, 0)
        now = time.time()
        cache_key = f"{'|'.join(chain)}|{depth}|{mr}|{tr}|{full_query}"

        hit = self._cache.get(cache_key)
        if hit and ttl and now - hit[0] < ttl:
            return hit[1], hit[2]

        results: list[dict] = []
        answer = ""
        for engine in chain:
            try:
                if engine == "bing":
                    results, answer = await self._search_bing(full_query, mr, tr)
                elif engine == "tavily":
                    results, answer = await self._search_tavily(full_query, mr, tr)
                else:
                    results, answer = await self._search_ddg(full_query, mr, tr)
            except Exception as e:
                logger.warning(f"[free_search] {engine} 搜索失败: {e}")
                results, answer = [], ""
            results = self._dedupe_results(results)
            if results or answer:
                if ttl:
                    if len(self._cache) > 256:
                        oldest = min(self._cache, key=lambda k: self._cache[k][0])
                        self._cache.pop(oldest, None)
                    self._cache[cache_key] = (now, results, answer)
                return results, answer
        return [], ""

    _WS_RE = re.compile(r"\s+")
    _CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    _SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
    _PAIR_RE = re.compile(r"([\ud800-\udbff])([\udc00-\udfff])")
    # 摘要开头的发布时间：Bing 网页版是「2025年2月28日 · 正文…」、360 是「4天前 - 正文…」
    _DATE_PREFIX = re.compile(
        r"^\s*("
        r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
        r"|(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
        r"|\d{1,2}\s*(?:分|分钟|小时|天|周|个?月|年)(?:前|之前)"
        r"|昨天|前天|今天|刚刚"
        r"|\d+\s*(?:min|mins|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago"
        r")\s*(?:T(?=\d)|[·•\-–—|,，、:：])\s*"
    )

    @classmethod
    def _sanitise(cls, text: str) -> str:
        """清掉不可显示字符（思路移植自 dsh-keyless-search 的 sanitiseText）。

        中文结果页里的 emoji 经常解码成 U+FFFD 或半个代理对，不清掉就是
        「真身曝光!<乱码>」这种脏字符混进上下文；控制字符同样要剔除。
        压缩空白是因为标题里带换行会破坏卡片排版。
        """
        text = (text or "").replace("\ufffd", "")
        # 先把成对的代理项合成真正的字符，再删孤立的半个——不这么处理的话
        # Python 会把 😀 当成两个独立代理项，连正常的 emoji 也被误删
        text = cls._PAIR_RE.sub(
            lambda m: chr(
                0x10000 + (ord(m.group(1)) - 0xD800) * 0x400 + (ord(m.group(2)) - 0xDC00)
            ),
            text,
        )
        text = cls._CTRL_RE.sub("", text)
        text = cls._SURROGATE_RE.sub("", text)
        return cls._WS_RE.sub(" ", text).strip()

    @classmethod
    def _peel_date(cls, text: str) -> tuple[str, str]:
        """把摘要开头的发布时间剥离出来，返回 (日期原文, 剩余摘要)。

        日期单独呈现给模型看时效性，正文里它就不再占位——既省字数，
        也让模型能判断这条是不是旧闻（减少它为求新而重复搜索）。
        """
        m = cls._DATE_PREFIX.match(text or "")
        if not m:
            return "", text
        date = cls._WS_RE.sub(" ", m.group(1)).strip()
        rest = text[m.end() :].strip()
        # 摘要本来就只有一串日期时不值得掏空
        return date, (rest or text)

    @classmethod
    def _denoise(cls, text: str) -> str:
        """抹掉提取正文里的导航/侧栏噪声。

        Tavily 的 content 常常把「百度百科信息框」「B 站推荐列表」这类满屏短行
        排在正文前面。如果直接截前 N 个字，截到的全是无关菜单、正文一个字没取到。
        这里先把连续出现的短行判为噪声丢掉，剩下的才是成句的内容。
        整页全是短行时（视频站的推荐列表等）清洗结果为空，直接返回空串，
        由调用方只保留标题和链接——这类摘要本来也没有信息量。
        """
        text = (text or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        keep: list[str] = []
        run: list[str] = []

        def flush() -> None:
            # 连续 4 行以内视为正常要点，超过才是导航区
            if len(run) <= 4:
                keep.extend(run)
            run.clear()

        for ln in lines:
            is_short = len(ln) < 24 and not re.search(r"[。！？!?；;：:]$", ln)
            if is_short:
                run.append(ln)
            else:
                flush()
                keep.append(ln)
        flush()

        return re.sub(r"\s+", " ", " ".join(keep)).strip()

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """按字符数截断（limit > 0 才生效），超长部分用省略号代替。"""
        text = (text or "").strip()
        if limit > 0 and len(text) > limit:
            return text[:limit].rstrip() + "…"
        return text

    def _format_results(self, query: str, results: list[dict]) -> str:
        """把搜索结果拼成给 LLM 看的文本。

        两道闸门防止污染上下文：
        - snippet_max_chars：单条摘要上限（挡住 Tavily 的整页正文）
        - max_total_chars：本次搜索总字数上限（挡住多条累加）
        """
        snip_limit = _cfg_int(self.config, "snippet_max_chars", 220, 0)
        total_limit = _cfg_int(self.config, "max_total_chars", 3500, 0)

        lines = [f"搜索关键词：{query}"]
        used = len(lines[0]) + 1
        for i, r in enumerate(results, 1):
            date, body = self._peel_date(self._sanitise(self._denoise(r["snippet"] or "")))
            head = f"[{i}] {self._clip(self._sanitise(r['title']), 80)}"
            if date:
                head += f"（{date}）"
            block = [head, f"链接: {self._sanitise(r['url'])}"]
            if body:
                block.append(f"摘要: {self._clip(body, snip_limit)}")
            block_len = sum(len(x) + 1 for x in block)
            if total_limit and used + block_len > total_limit:
                lines.append(
                    f"…（已达单次搜索字数上限 {total_limit}，后面 {len(results) - i + 1} 条省略）"
                )
                break
            lines.extend(block)
            used += block_len
        return "\n".join(lines)

    # ---------------- 会话级防护 ----------------

    def _current_session_chars(self, session: str, now: float) -> int:
        used, last = self._session_chars.get(session, (0, now))
        idle_minutes = _cfg_int(self.config, "session_budget_idle_minutes", 30, 0)
        if idle_minutes and now - last > idle_minutes * 60:
            return 0
        return used

    def _add_session_chars(self, session: str, amount: int) -> None:
        now = time.time()
        used = self._current_session_chars(session, now)
        self._session_chars[session] = (used + amount, now)

    def _maybe_cleanup(self, now: float) -> None:
        """定期回收长时间不活跃的会话状态，避免字典无限增长。"""
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        for key in list(self._calls):
            stamps = self._calls.get(key) or []
            if not stamps or now - max(stamps) > 3600:
                self._calls.pop(key, None)
        for key, (_used, last) in list(self._session_chars.items()):
            if now - last > 3600:
                self._session_chars.pop(key, None)

    def _guard(
        self,
        session: str,
        *,
        rate_key: str | None = None,
        calls_cfg: str = "max_calls_per_minute",
        enforce_budget: bool = True,
        label: str = "联网搜索",
    ) -> str | None:
        """会话级调用防护。返回 None 表示放行，否则返回给 LLM 的替代文本。

        - max_calls_per_minute / max_web_read_per_minute：每 60 秒最多几次（0 不限）
        - session_max_chars：累计注入上下文的字数上限（0 不限），空闲超过
          session_budget_idle_minutes 后自动重置
        """
        now = time.time()
        self._maybe_cleanup(now)
        key = rate_key or session
        max_calls = _cfg_int(self.config, calls_cfg, 8, 0)
        max_chars = _cfg_int(self.config, "session_max_chars", 15000, 0)

        stamps = [t for t in self._calls.get(key, []) if now - t < 60]
        if max_calls and len(stamps) >= max_calls:
            logger.info(
                f"[free_search] 会话 {key} 1 分钟内{label}已 {len(stamps)} 次，触发频率限制"
            )
            return (
                f"（本会话{label}已达频率上限：1 分钟最多 {max_calls} 次，暂时不能再用了。"
                "请基于已经拿到的信息回答用户；确实查不到就如实说明，不要编造。）"
            )

        used = self._current_session_chars(session, now)
        if enforce_budget and max_chars and used >= max_chars:
            logger.info(
                f"[free_search] 会话 {session} 累计搜索字数 {used} 已达上限 {max_chars}"
            )
            return (
                f"（本会话联网检索已累计向对话注入 {used}/{max_chars} 字，达到上限，"
                "为保证后续对话质量不再继续搜索。请综合已有信息作答，"
                "缺口如实告诉用户。）"
            )

        stamps.append(now)
        self._calls[key] = stamps
        if enforce_budget:
            self._session_chars[session] = (used, now)
        return None

    async def _search_text(
        self,
        query: str,
        *,
        site: str = "",
        time_range: str = "",
        max_results: int = 0,
        with_hint: bool = False,
    ) -> str:
        results, answer = await self._search(
            query, site=site, time_range=time_range, max_results=max_results
        )
        if not results and not answer:
            return ""
        parts: list[str] = []
        if answer:
            parts.append(f"综合答案：{answer}")
        if results:
            parts.append(self._format_results(query, results))
        text = "\n\n".join(parts)
        if with_hint or self.config.get("paraphrase_hint", True):
            text += _PARAPHRASE_HINT
        return text

    # ---------------- 对外暴露的能力 ----------------

    @filter.llm_tool("web_search")
    async def web_search(
        self,
        event: AstrMessageEvent,
        query: str,
        site: str = "",
        time_range: str = "",
        max_results: int = 0,
    ):
        """联网搜索实时信息（新闻、天气、价格、版本、资料等）。当问题涉及最新动态或你不确定的事实时调用。

        返回的结果已经做过精简（每条只保留短摘要），通常一次调用就够用。
        如果确实需要某条结果的完整内容，再单独用 web_read 打开它的链接。
        不要对同一个问题反复换词重复搜索。

        Args:
            query(string): 搜索关键词，尽量精炼
            site(string): 可选，只搜索指定站点（例如 zhihu.com）；留空表示不限
            time_range(string): 可选，只看近期结果，取值 day/week/month/year；留空表示不限
            max_results(int): 可选，返回条数（1-10）；0 表示使用插件配置
        """
        session = str(getattr(event, "unified_msg_origin", "") or "global")
        blocked = self._guard(session)
        if blocked:
            return blocked

        try:
            wanted = int(max_results or 0)
        except (TypeError, ValueError):
            wanted = 0
        tr = str(time_range or "").strip().lower()
        if tr not in _TIME_RANGES:
            tr = ""
        text = await self._search_text(
            query,
            site=str(site or "").strip(),
            time_range=tr,
            max_results=wanted,
        )
        if not text:
            return _EMPTY_SEARCH_HINT

        self._add_session_chars(session, len(text))
        return text

    @filter.llm_tool("web_read")
    async def web_read(self, event: AstrMessageEvent, url: str):
        """打开一个网页链接并读取正文文字内容。当用户分享了链接想了解内容，或需要进一步查看搜索结果里的某个来源时调用。

        Args:
            url(string): 完整的网页链接，以 http:// 或 https:// 开头
        """
        session = str(getattr(event, "unified_msg_origin", "") or "global")
        blocked = self._guard(
            session,
            rate_key=f"{session}#read",
            calls_cfg="max_web_read_per_minute",
            label="网页阅读",
        )
        if blocked:
            return blocked

        ok, text = await self._read_page(url)
        if ok:
            self._add_session_chars(session, len(text))
        return text

    @filter.command("搜索")
    async def search_command(self, event: AstrMessageEvent, query: str):
        """手动搜索，结果由 AI 口语化转述"""
        query = (query or "").strip()
        if not query:
            yield event.plain_result("用法：/搜索 关键词")
            return
        # 手动命令不受累计字数预算约束（用户主动要的），但仍受频率限制
        session = str(getattr(event, "unified_msg_origin", "") or "global")
        blocked = self._guard(
            f"{session}#cmd",
            rate_key=f"{session}#cmd",
            enforce_budget=False,
            label="搜索",
        )
        if blocked:
            yield event.plain_result("搜得太频繁啦，稍等一分钟再试～")
            return
        text = await self._search_text(query, with_hint=True)
        if not text:
            yield event.plain_result("没搜到结果（也可能是网络不可达），稍后再试试吧。")
            return
        yield event.request_llm(
            prompt=(
                f"用户手动发起了搜索，关键词：{query}\n\n"
                f"以下是搜索结果：\n{text}\n\n"
                "请根据这些结果，用自然的口语向用户转述搜索到的内容，突出要点；"
                "不要逐条罗列标题和链接，用户追问出处时再给出对应链接。"
            )
        )
