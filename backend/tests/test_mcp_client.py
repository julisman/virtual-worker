"""MCP client — connect to a real stdio echo server, list + call tools.

Spawns tests/mcp_echo_server.py as a subprocess via the official SDK, so this
exercises the full path: connect -> initialize -> list_tools -> call_tool, plus
the namespaced registry the chat loop consumes and the warm connection pool.

Sessions are pooled per event loop, so each test closes the pool (in its own
loop) before returning — otherwise the echo subprocesses would linger.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from agent import mcp_client

ECHO = str(Path(__file__).parent / "mcp_echo_server.py")


def _cfg():
    return {"transport": "stdio", "command": sys.executable, "args": [ECHO],
            "slug": "echo", "enabled": True}


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_build_registry_and_call():
    async def run():
        try:
            async with mcp_client.McpManager() as m:
                reg = await m.build_registry([_cfg()])
                assert "mcp__echo__echo" in reg
                entry = reg["mcp__echo__echo"]
                assert entry["schema"]["function"]["name"] == "mcp__echo__echo"
                return await entry["fn"](text="hello")
        finally:
            await mcp_client.close_pool()
    out = asyncio.run(run())
    assert "echo: hello" in out


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_list_server_tools():
    tools = asyncio.run(mcp_client.list_server_tools(_cfg()))
    names = [t["name"] for t in tools]
    assert "echo" in names


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_disabled_server_skipped():
    async def run():
        async with mcp_client.McpManager() as m:
            cfg = {**_cfg(), "enabled": False}
            return await m.build_registry([cfg])
    assert asyncio.run(run()) == {}


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_bad_server_is_not_fatal():
    async def run():
        try:
            async with mcp_client.McpManager() as m:
                bad = {"transport": "stdio", "command": "definitely-not-a-real-cmd-xyz",
                       "args": [], "slug": "bad", "enabled": True}
                return await m.build_registry([bad])
        finally:
            await mcp_client.close_pool()
    # connection fails -> skipped, returns empty registry (no raise), not cached
    assert asyncio.run(run()) == {}


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_hanging_server_times_out_and_is_skipped(monkeypatch):
    # A process that spawns fine but never speaks MCP: initialize() would hang
    # forever. The per-server timeout must bounce it (in ~1s, not the sleep's
    # lifetime) while a healthy server alongside it still registers — proving
    # one bad server can't stall the request and that connects run in parallel.
    monkeypatch.setenv("VW_MCP_CONNECT_TIMEOUT", "1")
    hang = {"transport": "stdio", "command": sys.executable,
            "args": ["-c", "import time; time.sleep(60)"], "slug": "hang", "enabled": True}

    async def run():
        try:
            async with mcp_client.McpManager() as m:
                return await m.build_registry([hang, _cfg()])
        finally:
            await mcp_client.close_pool()

    start = time.monotonic()
    reg = asyncio.run(run())
    elapsed = time.monotonic() - start
    assert "mcp__echo__echo" in reg          # healthy server survived
    assert not any(k.startswith("mcp__hang__") for k in reg)  # hung one skipped
    assert elapsed < 15                       # timed out fast, nowhere near sleep(60)


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_multiple_servers_all_register():
    # Two servers connect in parallel; both sets of tools land in one registry.
    async def run():
        try:
            async with mcp_client.McpManager() as m:
                a = {**_cfg(), "slug": "a", "id": "a"}
                b = {**_cfg(), "slug": "b", "id": "b"}
                reg = await m.build_registry([a, b])
                out_a = await reg["mcp__a__echo"]["fn"](text="x")
                out_b = await reg["mcp__b__echo"]["fn"](text="y")
                return reg, out_a, out_b
        finally:
            await mcp_client.close_pool()
    reg, out_a, out_b = asyncio.run(run())
    assert "mcp__a__echo" in reg and "mcp__b__echo" in reg
    assert "echo: x" in out_a and "echo: y" in out_b


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_warm_session_is_reused():
    # The whole point of the pool: a second build_registry reuses the live
    # session instead of respawning the server.
    async def run():
        try:
            pool = mcp_client.get_pool()
            await pool.build_registry([{**_cfg(), "id": "svc"}])
            first = pool._servers["svc"].session
            reg = await pool.build_registry([{**_cfg(), "id": "svc"}])
            second = pool._servers["svc"].session
            out = await reg["mcp__echo__echo"]["fn"](text="warm")
            return first is second, len(pool._servers), out
        finally:
            await mcp_client.close_pool()
    same, count, out = asyncio.run(run())
    assert same                 # reused, not respawned
    assert count == 1
    assert "echo: warm" in out


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_config_change_respawns():
    # Same pool key (server id) but a changed spawn config (different env) must
    # invalidate the warm session and respawn a fresh one.
    async def run():
        try:
            pool = mcp_client.get_pool()
            await pool.build_registry([{**_cfg(), "id": "svc"}])
            before = pool._servers["svc"].session
            changed = {**_cfg(), "id": "svc", "env": {**os.environ, "VW_TEST_MARKER": "1"}}
            await pool.build_registry([changed])
            after = pool._servers["svc"].session
            return before is not after
        finally:
            await mcp_client.close_pool()
    assert asyncio.run(run())    # fingerprint differed -> respawned


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_invalidate_drops_server():
    async def run():
        try:
            pool = mcp_client.get_pool()
            await pool.build_registry([{**_cfg(), "id": "svc"}])
            assert "svc" in pool._servers
            await pool.invalidate("svc")
            return "svc" in pool._servers
        finally:
            await mcp_client.close_pool()
    assert asyncio.run(run()) is False


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_connect_retries_transient_failure(monkeypatch):
    # A transient connect error (e.g. two `uvx` racing on the uv cache) is retried
    # once and then succeeds — the server still lands in the registry.
    real = mcp_client._open_session
    calls = {"n": 0}

    async def flaky(stack, cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("uv: failed to rename file … cache (os error 2)")
        return await real(stack, cfg)

    monkeypatch.setattr(mcp_client, "_open_session", flaky)

    async def run():
        try:
            reg = await mcp_client.get_pool().build_registry([{**_cfg(), "id": "svc"}])
            return calls["n"], "mcp__echo__echo" in reg
        finally:
            await mcp_client.close_pool()
    n, ok = asyncio.run(run())
    assert n == 2 and ok          # failed once, retried, connected


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_timeout_is_not_retried(monkeypatch):
    # A genuine timeout (what asyncio.timeout raises) means the server is
    # unresponsive — skip fast, don't burn a second full timeout retrying it.
    calls = {"n": 0}

    async def timing_out(stack, cfg):
        calls["n"] += 1
        raise TimeoutError()

    monkeypatch.setattr(mcp_client, "_open_session", timing_out)

    async def run():
        try:
            reg = await mcp_client.get_pool().build_registry([{**_cfg(), "id": "svc"}])
            return calls["n"], reg
        finally:
            await mcp_client.close_pool()
    n, reg = asyncio.run(run())
    assert n == 1 and reg == {}   # one attempt, timed out, skipped (no retry)


def test_graceful_when_sdk_absent(monkeypatch):
    monkeypatch.setattr(mcp_client, "_AVAILABLE", False)

    async def run():
        async with mcp_client.McpManager() as m:
            return await m.build_registry([_cfg()])
    assert asyncio.run(run()) == {}
    with pytest.raises(RuntimeError):
        asyncio.run(mcp_client.list_server_tools(_cfg()))


@pytest.mark.skipif(not mcp_client.mcp_available(), reason="mcp SDK not installed")
def test_allowed_tools_filters_registry():
    """A server's allowedTools list limits which tools agents see; empty/absent
    keeps every advertised tool (pre-allowlist behavior)."""
    async def run():
        try:
            async with mcp_client.McpManager() as m:
                cfg = {**_cfg(), "allowedTools": ["no_such_tool"]}
                filtered = await m.build_registry([cfg])
                unfiltered = await m.build_registry([_cfg()])
                return filtered, unfiltered
        finally:
            await mcp_client.close_pool()
    filtered, unfiltered = asyncio.run(run())
    assert "mcp__echo__echo" not in filtered          # not in the allowlist → hidden
    assert "mcp__echo__echo" in unfiltered            # no allowlist → exposed
