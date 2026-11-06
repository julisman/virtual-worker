"""DevTools-style capture tools: browser_console_messages / browser_network_requests.

The capture buffers are module state fed by Playwright event handlers; the read
tools only format them — so the tests populate the buffers directly and never
launch a browser.
"""

import asyncio

import pytest

from agent import browser_control as bc


@pytest.fixture(autouse=True)
def _clean_buffers():
    bc._console_log.clear()
    bc._net_log.clear()
    yield
    bc._console_log.clear()
    bc._net_log.clear()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- console ----

def test_console_empty():
    out = run(bc.browser_console_messages())
    assert "No console messages captured yet" in out
    assert "browser_open" in out


def test_console_messages_listed_oldest_first():
    bc._add_console("log", "first")
    bc._add_console("error", "boom", "https://app.local/x.js:12")
    out = run(bc.browser_console_messages())
    assert "Last 2 console message(s)" in out
    assert out.index("first") < out.index("boom")
    assert "[error] boom" in out
    assert "https://app.local/x.js:12" in out


def test_console_level_filter_and_warn_alias():
    bc._add_console("log", "chatter")
    bc._add_console("warning", "deprecated API")
    out = run(bc.browser_console_messages(level="warn"))
    assert "deprecated API" in out
    assert "chatter" not in out
    none = run(bc.browser_console_messages(level="info"))
    assert "No console messages at level 'info'" in none


def test_console_max_entries_and_text_cap():
    for i in range(60):
        bc._add_console("log", f"msg-{i}")
    out = run(bc.browser_console_messages())
    assert "msg-59" in out and "msg-9" not in out          # default 50 kept the tail
    bc._add_console("log", "x" * 2000)
    assert len(bc._console_log[-1]["text"]) == bc._MAX_TEXT


def test_console_ring_buffer_cap():
    for i in range(bc._MAX_LOG + 25):
        bc._add_console("log", f"m{i}")
    assert len(bc._console_log) == bc._MAX_LOG
    assert bc._console_log[0]["text"] == "m25"


# ---------------------------------------------------------------- network ----

def test_network_empty():
    out = run(bc.browser_network_requests())
    assert "No network requests captured yet" in out


def test_network_listing_and_failure():
    bc._add_net("get", "https://api.local/v1/items", 200, "xhr")
    bc._add_net("POST", "https://api.local/v1/items", 500, "fetch")
    bc._add_net("GET", "https://cdn.local/app.js", None, "script", "net::ERR_ABORTED")
    out = run(bc.browser_network_requests())
    assert "GET 200 xhr https://api.local/v1/items" in out
    assert "POST 500 fetch https://api.local/v1/items" in out
    assert "GET FAILED(net::ERR_ABORTED) script https://cdn.local/app.js" in out
    assert "no headers/bodies" in out


def test_network_match_url_substring_and_resource_type():
    bc._add_net("GET", "https://api.local/v1/items", 200, "xhr")
    bc._add_net("GET", "https://cdn.local/app.js", 200, "script")
    by_url = run(bc.browser_network_requests(match="cdn.local"))
    assert "app.js" in by_url and "v1/items" not in by_url
    by_type = run(bc.browser_network_requests(match="xhr"))
    assert "v1/items" in by_type and "app.js" not in by_type
    none = run(bc.browser_network_requests(match="nomatch-xyz"))
    assert "No captured requests match 'nomatch-xyz'" in none


def test_network_url_cap_and_entry_clamp():
    bc._add_net("GET", "https://x.local/" + "a" * 1000, 200, "xhr")
    assert len(bc._net_log[-1]["url"]) == bc._MAX_URL
    assert bc._clamp_entries("junk") == bc._MAX_LIST
    assert bc._clamp_entries(0) == 1
    assert bc._clamp_entries(10_000) == 200


# --------------------------------------------------------------- registry ----

def test_tools_registered():
    reg = bc.make_browser_tools()
    for name in ("browser_console_messages", "browser_network_requests"):
        assert name in reg
        assert reg[name]["schema"]["function"]["name"] == name
    from agent import tools
    assert "browser_console_messages" in tools.TOOLS
    assert "browser_network_requests" in tools.TOOLS
