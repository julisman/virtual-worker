"""Agent tools — pure/local bits (no network)."""

import asyncio

from agent import tools


def test_schemas_filters_unknown():
    s = tools.schemas(["web_search", "nope", "web_fetch"])
    names = [x["function"]["name"] for x in s]
    assert names == ["web_search", "web_fetch"]


def test_execute_unknown_tool():
    out = asyncio.run(tools.execute("nope", {}))
    assert "unknown tool" in out


def test_strip_tags():
    assert tools._strip_tags("<b>hi</b> &amp; <i>there</i>") == "hi & there"


def test_web_fetch_blocks_private_host():
    out = asyncio.run(tools.web_fetch("http://localhost:8888/secret"))
    assert "refused" in out.lower()
    out2 = asyncio.run(tools.web_fetch("http://127.0.0.1/x"))
    assert "refused" in out2.lower()


def test_registry_has_expected_tools():
    assert set(["web_search", "web_fetch"]).issubset(tools.TOOLS.keys())
