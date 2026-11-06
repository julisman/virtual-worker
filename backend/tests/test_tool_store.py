"""Custom tool store CRUD + the make_custom_tools factory / SSRF guard."""

import asyncio

import pytest

import config
import storage.tool_store as ts
from agent import custom_tools


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return ts


# --- CRUD ------------------------------------------------------------------ #

def test_create_and_get(store):
    t = store.create_tool({
        "name": "lookup_order", "description": "Look up an order",
        "method": "GET", "url": "https://api.example.com/orders",
        "headers": {"Authorization": "Bearer secret"},
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]},
    })
    assert t["name"] == "lookup_order"
    assert t["transport"] == "http"
    assert t["method"] == "GET"
    assert t["enabled"] is True
    assert t["headers"]["Authorization"] == "Bearer secret"
    got = store.get_tool(t["id"])
    assert got["parameters"]["properties"]["order_id"]["type"] == "string"


def test_name_must_be_valid_identifier(store):
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "Bad Name"})
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "9leading"})


def test_name_cannot_shadow_builtin_or_mcp(store):
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "browser_search"})
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "mcp__x__y"})


def test_name_unique(store):
    store.create_tool({"name": "quote", "url": "https://api.example.com/q"})
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "quote", "url": "https://api.example.com/q2"})


def test_method_validated(store):
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "weird", "method": "TRACE"})


def test_update_partial(store):
    t = store.create_tool({"name": "t1", "url": "https://a.example.com"})
    upd = store.update_tool(t["id"], {"enabled": False})
    assert upd["enabled"] is False
    assert upd["url"] == "https://a.example.com"  # untouched


def test_delete(store):
    t = store.create_tool({"name": "t1"})
    assert store.delete_tool(t["id"]) is True
    assert store.get_tool(t["id"]) is None
    assert store.delete_tool("missing") is False


def test_get_tools_preserves_order(store):
    a = store.create_tool({"name": "aa"})
    b = store.create_tool({"name": "bb"})
    out = store.get_tools([b["id"], a["id"]])
    assert [t["name"] for t in out] == ["bb", "aa"]


# --- fixed parameters ------------------------------------------------------ #

def test_fixed_params_round_trip(store):
    t = store.create_tool({
        "name": "invoke", "method": "POST", "url": "https://api.example.com/x",
        "fixedParams": {"deployment_id": "abc123", "source": "ves"},
    })
    assert t["fixedParams"] == {"deployment_id": "abc123", "source": "ves"}
    assert store.get_tool(t["id"])["fixedParams"]["source"] == "ves"


def test_fixed_params_default_empty(store):
    t = store.create_tool({"name": "plain", "url": "https://api.example.com/x"})
    assert t["fixedParams"] == {}


def test_fixed_params_must_be_scalar(store):
    with pytest.raises(ts.ToolError):
        store.create_tool({"name": "bad_fixed", "fixedParams": {"k": {"nested": 1}}})


def test_fixed_params_merged_into_request(store, monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return FakeResp()

    monkeypatch.setattr(custom_tools.httpx, "AsyncClient", FakeClient)
    t = store.create_tool({
        "name": "invoke2", "method": "POST", "url": "https://api.example.com/x",
        "allowPrivate": True,  # skip DNS-based SSRF guard for a public host anyway
        "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
        "fixedParams": {"deployment_id": "abc123"},
    })
    reg = custom_tools.make_custom_tools([t["id"]])
    asyncio.run(reg["invoke2"]["fn"](input="hello", deployment_id="model-tries-to-override"))
    # Model arg present; fixed value wins over a colliding model arg.
    assert captured["json"]["input"] == "hello"
    assert captured["json"]["deployment_id"] == "abc123"


def test_dotted_params_nest_in_body(store, monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kwargs):
            captured.update(kwargs)
            return FakeResp()

    monkeypatch.setattr(custom_tools.httpx, "AsyncClient", FakeClient)
    t = store.create_tool({
        "name": "invoke3", "method": "POST", "url": "https://api.example.com/x",
        "allowPrivate": True,
        "parameters": {"type": "object", "properties": {"message.input_str": {"type": "string"}}},
        "fixedParams": {"endpoint_deployment_hash_id": "m53", "user_id": "u1"},
    })
    reg = custom_tools.make_custom_tools([t["id"]])
    asyncio.run(reg["invoke3"]["fn"](**{"message.input_str": "Hello, world!"}))
    assert captured["json"] == {
        "endpoint_deployment_hash_id": "m53",
        "user_id": "u1",
        "message": {"input_str": "Hello, world!"},
    }


# --- make_custom_tools / schema -------------------------------------------- #

def test_make_custom_tools_builds_schema(store):
    t = store.create_tool({
        "name": "get_weather", "description": "Get the weather",
        "url": "https://api.example.com/weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    })
    reg = custom_tools.make_custom_tools([t["id"]])
    assert "get_weather" in reg
    schema = reg["get_weather"]["schema"]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_weather"
    assert schema["function"]["parameters"]["properties"]["city"]["type"] == "string"
    assert callable(reg["get_weather"]["fn"])


def test_make_custom_tools_skips_disabled(store):
    t = store.create_tool({"name": "off_tool", "url": "https://x.example.com", "enabled": False})
    assert custom_tools.make_custom_tools([t["id"]]) == {}


# --- SSRF guard ------------------------------------------------------------ #

def test_ssrf_guard_blocks_loopback(store):
    t = store.create_tool({"name": "internal", "url": "http://127.0.0.1:8001/admin"})
    reg = custom_tools.make_custom_tools([t["id"]])
    result = asyncio.run(reg["internal"]["fn"]())
    assert "refused" in result
    assert "private/loopback" in result


def test_ssrf_guard_blocks_localhost(store):
    t = store.create_tool({"name": "local2", "url": "http://localhost/x"})
    reg = custom_tools.make_custom_tools([t["id"]])
    assert "refused" in asyncio.run(reg["local2"]["fn"]())


def test_ssrf_guard_bypass_when_allowed(store):
    # allowPrivate opt-in passes the guard; the call then fails to connect (no
    # server) — proving the guard didn't short-circuit it.
    t = store.create_tool({
        "name": "local3", "url": "http://127.0.0.1:59999/x", "allowPrivate": True,
    })
    reg = custom_tools.make_custom_tools([t["id"]])
    result = asyncio.run(reg["local3"]["fn"]())
    assert "refused" not in result
    assert "error" in result.lower()
