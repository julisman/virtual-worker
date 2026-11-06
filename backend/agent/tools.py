"""Tool registry for the agentic chat loop.

Tools are plain async functions + an OpenAI function schema. Add more here (or an
MCP bridge / code sandbox) without touching the loop. Current tools:

  - browser_search : query a self-hosted **SearXNG** instance (open-source
                     metasearch) over its JSON API — no API key, no paid third
                     party, and no fragile engine-scraping. Set VW_SEARXNG_URL.
  - browser_fetch  : open a URL in a real headless **Chromium** (Playwright) and
                     return its rendered text — handles JavaScript pages a plain
                     HTTP GET can't.

Why SearXNG for search: scraping Google/Bing/DuckDuckGo directly is unreliable
(bot detection, shifting markup, rate limits). SearXNG runs on your machine,
aggregates + rotates engines, and hands back clean JSON. Fetch still uses a real
browser because rendering an arbitrary page is exactly what a browser is for.

``_is_private_host`` is the SSRF guard reused by ``delivery.py`` (webhooks).
"""

from __future__ import annotations

import asyncio
import html as htmllib
import ipaddress
import logging
import os
import re
import socket
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Config (env, local-first).
_SEARXNG_URL = (os.environ.get("VW_SEARXNG_URL") or "").strip().rstrip("/")
_HEADLESS = (os.environ.get("VW_BROWSER_HEADLESS") or "1") != "0"
_NAV_MS = 25_000            # browser navigation timeout (fetch)
_SETTLE_MS = 3_000          # extra wait for JS to settle (best-effort)

_NEEDS_SEARXNG = (
    "web search needs a SearXNG instance (open source, no API key). Virtual Worker can run one "
    "for you: install it once under ~/.virtual-worker/searxng (see docs / run.sh), and the "
    "backend auto-starts it — no URL to set. Or point Virtual Worker at an existing instance "
    "with VW_SEARXNG_URL, or per-agent under Agents → Web search."
)


def _managed_searxng_url() -> str:
    """The backend-managed SearXNG URL, if it's installed + managed (else '')."""
    try:
        import config
        from runtime import searxng_manager
        if config.MANAGED_SEARXNG and searxng_manager.installed():
            return searxng_manager.base_url()
    except Exception:  # noqa: BLE001
        pass
    return ""
_NEEDS_BROWSER = (
    "browser_fetch needs Playwright + a Chromium build. Install once:\n"
    "  cd backend && .venv/bin/pip install playwright "
    "&& .venv/bin/python -m playwright install chromium"
)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(s).strip()


# --------------------------------------------------------------------------- #
# SSRF guard (also imported by delivery.py)
# --------------------------------------------------------------------------- #

def _is_private_host(url: str) -> bool:
    """Block SSRF targets (localhost / private / link-local / reserved / …).

    Canonical guard, reused by delivery.py and custom_tools.py. Fails **closed**:
    an unparseable URL, an empty/unresolvable host, or any resolution error is
    treated as blocked, so an odd or hostile input never gets a free pass to
    reach out. (An earlier version failed open on exception — that let a host
    that errored during resolution through.)"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return True
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # can't resolve -> don't reach out
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


async def _is_private_host_async(url: str) -> bool:
    """The same guard with the blocking DNS resolution moved off the event
    loop — it runs per subresource during a page fetch, and one slow-resolving
    host would otherwise stall every coroutine in the process."""
    return await asyncio.to_thread(_is_private_host, url)


# --------------------------------------------------------------------------- #
# browser_search — pluggable provider, chosen per-agent (see make_search_tool)
#   searxng     : self-hosted metasearch JSON API (no key)         [default]
#   tavily      : api.tavily.com          (needs an API key)
#   brave       : Brave Search API        (needs an API key)
#   duckduckgo  : DuckDuckGo HTML scrape  (no key, best-effort)
# Each returns a list of {title, url, snippet}; make_search_tool formats them.
# --------------------------------------------------------------------------- #

async def _search_searxng(url: str, query: str, n: int) -> list[dict]:
    params = {"q": query, "format": "json", "language": "en", "safesearch": "0"}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA},
                                 follow_redirects=True) as c:
        r = await c.get(url + "/search", params=params)
        r.raise_for_status()
        data = r.json()
    return [{"title": _strip_tags(str(x.get("title") or "")), "url": x.get("url") or "",
             "snippet": _strip_tags(str(x.get("content") or ""))}
            for x in (data.get("results") or [])[:n]]


async def _search_tavily(key: str, query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query, "max_results": n})
        r.raise_for_status()
        return [{"title": x.get("title") or "", "url": x.get("url") or "",
                 "snippet": x.get("content") or ""} for x in r.json().get("results", [])]


async def _search_brave(key: str, query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"X-Subscription-Token": key}) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": n})
        r.raise_for_status()
        return [{"title": x.get("title") or "", "url": x.get("url") or "",
                 "snippet": _strip_tags(x.get("description") or "")}
                for x in r.json().get("web", {}).get("results", [])]


async def _search_ddg(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA},
                                 follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
        html = r.text
    anchors = re.findall(r'class="result__a"[^>]*href="(.*?)".*?>(.*?)</a>', html, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out: list[dict] = []
    for i, (href, title) in enumerate(anchors[:n]):
        if "uddg=" in href:  # DDG wraps targets as /l/?uddg=<encoded>
            href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        out.append({"title": _strip_tags(title), "url": href,
                    "snippet": _strip_tags(snippets[i]) if i < len(snippets) else ""})
    return out


_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_search",
        "description": "Search the web for up-to-date information. Returns titles, URLs, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "the search query"}},
            "required": ["query"],
        },
    },
}


def make_search_tool(cfg: dict | None = None) -> dict:
    """Build the ``browser_search`` tool for one agent's web-search settings:
    ``{provider, apiKey, searxngUrl}``. ``provider`` picks the backend; SearXNG
    falls back to the global ``VW_SEARXNG_URL`` when no per-agent URL is set."""
    cfg = cfg or {}
    provider = (cfg.get("provider") or "searxng").strip().lower()
    api_key = (cfg.get("apiKey") or "").strip()
    cfg_url = (cfg.get("searxngUrl") or "").strip().rstrip("/")

    async def browser_search(query: str = "", max_results: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "browser_search needs a 'query'."
        n = max(1, min(int(max_results or 5), 10))
        try:
            if provider == "tavily":
                if not api_key:
                    return "web search: Tavily is selected but no API key is set (agent → Web search)."
                results = await _search_tavily(api_key, query, n)
            elif provider == "brave":
                if not api_key:
                    return "web search: Brave is selected but no API key is set (agent → Web search)."
                results = await _search_brave(api_key, query, n)
            elif provider == "duckduckgo":
                results = await _search_ddg(query, n)
            else:  # searxng — per-agent URL, else global env, else the managed instance
                searxng_url = cfg_url or _SEARXNG_URL or _managed_searxng_url()
                if not searxng_url:
                    return _NEEDS_SEARXNG
                results = await _search_searxng(searxng_url, query, n)
        except Exception as exc:  # noqa: BLE001
            return f"browser_search failed ({provider}): {exc}"
        if not results:
            return f"No results for: {query}"
        lines = [f"Search results for: {query}\n"]
        for i, res in enumerate(results, 1):
            lines.append(f"{i}. {res['title']}\n   {res['url']}\n   {res['snippet']}")
        return "\n".join(lines)

    return {"fn": browser_search, "kind": "web", "schema": _SEARCH_SCHEMA}


# --------------------------------------------------------------------------- #
# Managed headless browser (browser_fetch) — lazy, reused, relaunched if it dies.
# --------------------------------------------------------------------------- #

_pw = None          # the Playwright driver
_browser = None     # the shared Chromium instance
_lock = asyncio.Lock()

# Minimal "stealth": some sites drop clients that expose navigator.webdriver.
_STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def browser_available() -> bool:
    """True if the Playwright Python package is importable. (The Chromium binary
    is checked lazily at launch — a missing browser surfaces as a launch error
    with the install hint.)"""
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


async def _get_browser():
    """Return a live shared Chromium, launching (or relaunching) as needed."""
    global _pw, _browser
    from playwright.async_api import async_playwright
    async with _lock:
        if _browser is not None:
            try:
                if _browser.is_connected():
                    return _browser
            except Exception:
                pass
        if _pw is None:
            _pw = await async_playwright().start()
        launch_kw = dict(
            headless=_HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        try:
            import config
            launch_kw["headless"] = config.browser_fetch_headless()
            channel = config.browser_channel()
            if channel:  # drive system Chrome instead of bundled Chromium
                launch_kw["channel"] = channel
        except Exception:  # noqa: BLE001
            pass
        _browser = await _pw.chromium.launch(**launch_kw)
        return _browser


async def _new_context(browser):
    """A fresh, US/English browser context with the webdriver flag masked."""
    ctx = await browser.new_context(
        user_agent=_UA, locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
    try:
        await ctx.add_init_script(_STEALTH)
    except Exception:  # noqa: BLE001
        pass
    return ctx


async def close_browser() -> None:
    """Tear down the shared browser (call on app shutdown)."""
    global _pw, _browser
    async with _lock:
        b, p, _browser, _pw = _browser, _pw, None, None
    if b is not None:
        try:
            await b.close()
        except Exception:  # noqa: BLE001
            pass
    if p is not None:
        try:
            await p.stop()
        except Exception:  # noqa: BLE001
            pass


async def browser_fetch(url: str = "", max_chars: int = 6000) -> str:
    url = (url or "").strip()
    if not url:
        return "browser_fetch needs a 'url'."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if await _is_private_host_async(url):
        return "browser_fetch refused: that host is private/loopback."
    if not browser_available():
        return _NEEDS_BROWSER
    try:
        browser = await _get_browser()
        ctx = await _new_context(browser)
        try:
            page = await ctx.new_page()

            # Re-check every request (redirect hops + subresources) against the
            # SSRF guard: the initial-URL check above can't see a 302 to
            # 127.0.0.1 or an <img src=http://169.254.169.254/>.
            async def _guard(route):
                if await _is_private_host_async(route.request.url):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _guard)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_MS)
            if await _is_private_host_async(page.url):  # defense in depth: where we landed
                return "browser_fetch refused: redirected to a private/loopback host."
            try:  # let client-side rendering settle; not fatal if it doesn't
                await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
            except Exception:  # noqa: BLE001
                pass
            text = await page.inner_text("body")
            if not (text or "").strip():  # headless fallback
                text = await page.text_content("body")
        finally:
            await ctx.close()
    except Exception as exc:  # noqa: BLE001
        return f"browser_fetch failed: {exc}"
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text or "")).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…(+{len(text) - max_chars} chars truncated)"
    return f"Content of {url}:\n\n{text or '(no readable text)'}"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TOOLS: dict[str, dict] = {
    # Default (no per-agent config): SearXNG via VW_SEARXNG_URL. compose() swaps
    # this for a per-agent make_search_tool(agent["webSearch"]) when the agent runs.
    "browser_search": make_search_tool(None),
    "browser_fetch": {
        "fn": browser_fetch,
        "kind": "web",
        "schema": {
            "type": "function",
            "function": {
                "name": "browser_fetch",
                "description": "Open a web page in a real browser and return its rendered, "
                               "readable text (handles JavaScript pages). Use after browser_search.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "the URL to open"}},
                    "required": ["url"],
                },
            },
        },
    },
}

# Interactive browser control (browser_open / _snapshot / _click / _type / …) —
# a separate, visible, persistent-profile browser the agent drives step by step.
# Registered here so each tool is selectable per-agent by name, like browser_fetch.
from agent import browser_control  # noqa: E402  (imported late: reuses _NEEDS_BROWSER above)
TOOLS.update(browser_control.make_browser_tools())


def schemas(names: list[str]) -> list[dict]:
    return [TOOLS[n]["schema"] for n in names if n in TOOLS]


async def execute(name: str, args: dict) -> str:
    tool = TOOLS.get(name)
    if not tool:
        return f"unknown tool: {name}"
    try:
        return await tool["fn"](**(args or {}))
    except TypeError as exc:
        return f"bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool %s failed: %s", name, exc)
        return f"{name} error: {exc}"
