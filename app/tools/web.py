"""Web tools — give the Run agent eyes on the outside world.

  • web_fetch(url)         — fetch a page and return its readable text. Keyless.
  • web_search(query)      — search the web. Keyless by default (DuckDuckGo HTML);
                             uses Tavily/Brave when a key is configured app-side.

Both are async `handler(args, ctx) -> str`. Nothing here is privileged: the optional
search key lives in the app's own backend config (the data partition), NEVER in the
kernel. `httpx` is already a dependency.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import urllib.parse
from html.parser import HTMLParser

import httpx

_UA = "Mozilla/5.0 (compatible; QuineBot/1.0; +https://quine.dev)"
_FETCH_LIMIT = 12000     # chars of page text returned to the model
_TIMEOUT = 20.0
_MAX_REDIRECTS = 5


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


# ── HTML → readable text ────────────────────────────────────────────────────────────
_BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "li", "tr",
               "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"}
_SKIP_TAGS = {"script", "style", "noscript", "head", "svg", "nav", "template"}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style and inserting breaks at block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of spaces/tabs, then trim blank lines to a single newline.
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n[ \t]*\n+", "\n\n", raw)
        return raw.strip()


def _html_to_text(body: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(body)
    except Exception:
        # Fall back to a crude tag strip if the parser chokes on malformed markup.
        return html.unescape(re.sub(r"<[^>]+>", " ", body)).strip()
    return parser.text()


# ── web_fetch (with an SSRF guard) ──────────────────────────────────────────────────
def _ip_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable ⇒ treat as unsafe
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


class _SSRFBlocked(Exception):
    """A host resolved to a private / loopback / link-local / reserved address."""


async def _pin_ip(host: str, port: int) -> str | None:
    """SSRF guard + DNS-rebinding pin (P1.8). Resolve `host` ONCE and return a single validated
    IP for the caller to connect to directly — so httpx can't re-resolve at connect time to a
    different (internal) address between this check and the socket (e.g. a low-TTL record that
    answers public for the check, then 169.254.169.254 for the fetch).

    Raises _SSRFBlocked if ANY resolved address is internal (one poisoned answer is enough to
    refuse — the metadata endpoint and internal services must stay unreachable). Returns None
    when the host can't be resolved, so the caller fetches unpinned: the connect then simply
    fails, which keeps the tool working offline / without DNS."""
    if not host:
        raise _SSRFBlocked
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except Exception:
        return None
    ips = [str(info[4][0]) for info in infos]
    if any(_ip_blocked(ip) for ip in ips):
        raise _SSRFBlocked
    return ips[0] if ips else None


async def web_fetch(args: dict, ctx) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "error: a url is required"
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    # Redirects are followed MANUALLY so the SSRF guard re-runs on every hop — otherwise a
    # public URL could 30x-redirect to an internal address and slip past the initial check.
    r = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False,
                                     headers={"User-Agent": _UA}) as c:
            for _ in range(_MAX_REDIRECTS + 1):
                parsed = urllib.parse.urlparse(url)
                scheme = parsed.scheme.lower()
                if scheme not in ("http", "https"):
                    return f"error: refusing to fetch non-HTTP URL ({parsed.scheme or 'no scheme'})"
                host = parsed.hostname or ""
                port = parsed.port or (443 if scheme == "https" else 80)
                try:
                    pin = await _pin_ip(host, port)
                except _SSRFBlocked:
                    return ("error: refusing to fetch a private, loopback, or link-local "
                            "address (blocked to prevent server-side request forgery)")
                if pin is not None:
                    # Connect to the validated IP, but keep the Host header and (for TLS) the SNI
                    # + cert verification on the REAL hostname — so a rebind can't reach inside.
                    host_hdr = host if parsed.port is None else f"{host}:{parsed.port}"
                    req = c.build_request("GET", httpx.URL(url).copy_with(host=pin),
                                          headers={"Host": host_hdr})
                    req.extensions["sni_hostname"] = host
                    r = await c.send(req)
                else:
                    r = await c.get(url)  # unresolved host → unpinned (connect just fails)
                location = r.headers.get("location")
                if getattr(r, "is_redirect", False) and location:
                    url = str(httpx.URL(url).join(location))
                    continue
                break
            else:
                return "error: too many redirects"
    except Exception as exc:
        return f"error: could not fetch {url}: {exc}"
    if r is None:
        return "error: could not fetch the URL"
    ctype = r.headers.get("content-type", "")
    body = r.text
    text = _html_to_text(body) if ("html" in ctype or body.lstrip()[:1] == "<") else body
    head = f"[{r.status_code}] {r.url}\n"
    if len(text) > _FETCH_LIMIT:
        text = text[:_FETCH_LIMIT] + f"\n…[truncated {len(text) - _FETCH_LIMIT} chars]"
    return head + (text or "(no readable text)")


# ── web_search ──────────────────────────────────────────────────────────────────────
def _search_cfg(ctx) -> dict:
    """Search provider config from the app's backend config: {provider, api_key}."""
    if getattr(ctx, "config_get", None) is None:
        return {}
    try:
        return (ctx.config_get() or {}).get("search", {}) or {}
    except Exception:
        return {}


def _fmt_results(results: list[dict]) -> str:
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(untitled)"
        url = (r.get("url") or "").strip()
        snippet = " ".join((r.get("snippet") or "").split())
        lines.append(f"{i}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
    return "\n".join(lines)


async def _tavily(query: str, key: str, count: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query, "max_results": count})
        r.raise_for_status()
        data = r.json()
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content")}
            for x in (data.get("results") or [])][:count]


async def _brave(query: str, key: str, count: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT,
                                 headers={"X-Subscription-Token": key,
                                          "Accept": "application/json"}) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": count})
        r.raise_for_status()
        data = r.json()
    web = (data.get("web") or {}).get("results") or []
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description")}
            for x in web][:count]


_DDG_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_DDG_SNIP_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _ddg_href(href: str) -> str:
    """DuckDuckGo HTML wraps targets in a redirect carrying the real url in `uddg`."""
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href).query
            uddg = urllib.parse.parse_qs(q).get("uddg")
            if uddg:
                return urllib.parse.unquote(uddg[0])
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


async def _duckduckgo(query: str, count: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
        body = r.text
    links = _DDG_LINK_RE.findall(body)
    snips = _DDG_SNIP_RE.findall(body)
    out: list[dict] = []
    for i, (href, title) in enumerate(links[:count]):
        out.append({
            "title": _clean(title),
            "url": _ddg_href(href),
            "snippet": _clean(snips[i]) if i < len(snips) else "",
        })
    return out


async def web_search(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "error: a query is required"
    count = max(1, min(10, int(args.get("count", 5) or 5)))
    cfg = _search_cfg(ctx)
    provider = (cfg.get("provider") or "").lower()
    key = cfg.get("api_key") or ""
    try:
        if provider == "tavily" and key:
            results = await _tavily(query, key, count)
        elif provider == "brave" and key:
            results = await _brave(query, key, count)
        else:
            results = await _duckduckgo(query, count)
    except Exception as exc:
        return f"error: search failed: {exc}"
    return _fmt_results(results)


TOOLS = {
    "web_fetch": {
        "schema": _schema(
            "web_fetch",
            "Fetch a web page (or any URL) and return its readable text. Use this to read "
            "documentation, articles, or any link the user mentions.",
            {"url": {"type": "string", "description": "the URL to fetch"}},
            ["url"],
        ),
        "handler": web_fetch,
    },
    "web_search": {
        "schema": _schema(
            "web_search",
            "Search the web and return the top results (title, url, snippet). Use this to "
            "find current information, then web_fetch a result to read it in full.",
            {"query": {"type": "string", "description": "the search query"},
             "count": {"type": "integer", "description": "how many results (1–10, default 5)"}},
            ["query"],
        ),
        "handler": web_search,
    },
}
