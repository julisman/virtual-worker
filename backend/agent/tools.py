"""Tool registry for the agentic chat loop.

Tools are plain async functions + an OpenAI function schema. Add more here (or an
MCP bridge / code sandbox) without touching the loop. Current tools:

  - web_search : DuckDuckGo (no key) or Tavily/Brave if a key is set
  - web_fetch  : fetch a URL and return readable text

No third-party deps — just httpx + stdlib HTML scrubbing.
"""

from __future__ import annotations

import html as htmllib
import ipaddress
import logging
import os
import re
import socket
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; Virtual Worker/0.1; +local)"
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(s).strip()


# --------------------------------------------------------------------------- #
# web_search
# --------------------------------------------------------------------------- #

async def _search_tavily(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": TAVILY_API_KEY, "query": query, "max_results": n})
        r.raise_for_status()
        return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content", "")}
                for x in r.json().get("results", [])]


async def _search_brave(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"X-Subscription-Token": BRAVE_API_KEY}) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": n})
        r.raise_for_status()
        web = r.json().get("web", {}).get("results", [])
        return [{"title": x.get("title"), "url": x.get("url"), "snippet": _strip_tags(x.get("description", ""))}
                for x in web]


async def _search_ddg(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}, follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
    html = r.text
    anchors = re.findall(r'class="result__a"[^>]*href="(.*?)".*?>(.*?)</a>', html, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out = []
    for i, (href, title) in enumerate(anchors[:n]):
        # DDG wraps targets as /l/?uddg=<encoded>
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        out.append({"title": _strip_tags(title), "url": href,
                    "snippet": _strip_tags(snippets[i]) if i < len(snippets) else ""})
    return out


async def web_search(query: str, max_results: int = 5) -> str:
    n = max(1, min(max_results, 8))
    try:
        if TAVILY_API_KEY:
            results = await _search_tavily(query, n)
        elif BRAVE_API_KEY:
            results = await _search_brave(query, n)
        else:
            results = await _search_ddg(query, n)
    except Exception as exc:  # noqa: BLE001
        return f"web_search failed: {exc}"
    if not results:
        return f"No results for: {query}"
    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# web_fetch
# --------------------------------------------------------------------------- #

def _is_private_host(url: str) -> bool:
    """Block obvious SSRF targets (localhost / private / link-local)."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if host in ("localhost",):
            return True
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except Exception:
        return False
    return False


async def web_fetch(url: str, max_chars: int = 6000) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if _is_private_host(url):
        return "web_fetch refused: that host is private/loopback."
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            html = r.text
    except Exception as exc:  # noqa: BLE001
        return f"web_fetch failed: {exc}"
    # crude readable-text extraction
    html = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", " ", html, flags=re.I)
    text = _strip_tags(html)
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text))
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…(+{len(text) - max_chars} chars truncated)"
    return f"Content of {url}:\n\n{text}"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TOOLS: dict[str, dict] = {
    "web_search": {
        "fn": web_search,
        "schema": {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for up-to-date information. Returns titles, URLs, and snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "the search query"}},
                    "required": ["query"],
                },
            },
        },
    },
    "web_fetch": {
        "fn": web_fetch,
        "schema": {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a web page and return its readable text. Use after web_search to read a result.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "the URL to fetch"}},
                    "required": ["url"],
                },
            },
        },
    },
}


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
