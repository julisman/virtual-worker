"""Agent tools — pure/local bits (no network)."""

import asyncio

from agent import tools


def test_schemas_filters_unknown():
    s = tools.schemas(["browser_search", "nope", "browser_fetch"])
    names = [x["function"]["name"] for x in s]
    assert names == ["browser_search", "browser_fetch"]


def test_execute_unknown_tool():
    out = asyncio.run(tools.execute("nope", {}))
    assert "unknown tool" in out


def test_strip_tags():
    assert tools._strip_tags("<b>hi</b> &amp; <i>there</i>") == "hi & there"


def test_browser_fetch_blocks_private_host():
    # SSRF guard runs before any browser launch, so it holds even with no Chromium.
    out = asyncio.run(tools.browser_fetch("http://localhost:8888/secret"))
    assert "refused" in out.lower()
    out2 = asyncio.run(tools.browser_fetch("http://127.0.0.1/x"))
    assert "refused" in out2.lower()


def test_browser_fetch_degrades_without_playwright(monkeypatch):
    # A public URL passes the SSRF check; with no Chromium it returns an install hint.
    # The guard is stubbed so the test never does live DNS (fails closed on VPNs
    # whose resolver can't see example.com).
    monkeypatch.setattr(tools, "_is_private_host", lambda url: False)
    monkeypatch.setattr(tools, "browser_available", lambda: False)
    out = asyncio.run(tools.browser_fetch("https://example.com"))
    assert "playwright" in out.lower()


def test_browser_search_needs_searxng(monkeypatch):
    # SearXNG selected but no URL anywhere (per-agent, env, or managed) → setup hint.
    monkeypatch.setattr(tools, "_SEARXNG_URL", "")
    monkeypatch.setattr(tools, "_managed_searxng_url", lambda: "")
    tool = tools.make_search_tool({"provider": "searxng", "searxngUrl": ""})
    out = asyncio.run(tool["fn"](query="hello"))
    assert "searxng" in out.lower()


def test_browser_search_provider_needs_key():
    # Tavily/Brave selected without a key → a clear "set a key" message, no call.
    for p in ("tavily", "brave"):
        tool = tools.make_search_tool({"provider": p, "apiKey": ""})
        out = asyncio.run(tool["fn"](query="hello"))
        assert "key" in out.lower()


def test_browser_search_formats_searxng_results(monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{"title": "Example", "url": "https://e.com", "content": "a snippet"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(tools.httpx, "AsyncClient", _Client)
    tool = tools.make_search_tool({"provider": "searxng", "searxngUrl": "http://searx.local"})
    out = asyncio.run(tool["fn"](query="q"))
    assert "Example" in out and "https://e.com" in out and "a snippet" in out


def test_registry_has_expected_tools():
    assert set(["browser_search", "browser_fetch"]).issubset(tools.TOOLS.keys())
