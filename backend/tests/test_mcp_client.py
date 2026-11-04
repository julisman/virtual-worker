"""MCP client — connect to a real stdio echo server, list + call tools.

Spawns tests/mcp_echo_server.py as a subprocess via the official SDK, so this
exercises the full path: connect -> initialize -> list_tools -> call_tool, plus
the namespaced registry the chat loop consumes.
"""

import asyncio
import sys
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
        async with mcp_client.McpManager() as m:
            reg = await m.build_registry([_cfg()])
            assert "mcp__echo__echo" in reg
            entry = reg["mcp__echo__echo"]
            assert entry["schema"]["function"]["name"] == "mcp__echo__echo"
            out = await entry["fn"](text="hello")
            return out
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
        async with mcp_client.McpManager() as m:
            bad = {"transport": "stdio", "command": "definitely-not-a-real-cmd-xyz",
                   "args": [], "slug": "bad", "enabled": True}
            return await m.build_registry([bad])
    # connection fails -> skipped, returns empty registry (no raise)
    assert asyncio.run(run()) == {}


def test_graceful_when_sdk_absent(monkeypatch):
    monkeypatch.setattr(mcp_client, "_AVAILABLE", False)

    async def run():
        async with mcp_client.McpManager() as m:
            return await m.build_registry([_cfg()])
    assert asyncio.run(run()) == {}
    with pytest.raises(RuntimeError):
        asyncio.run(mcp_client.list_server_tools(_cfg()))
