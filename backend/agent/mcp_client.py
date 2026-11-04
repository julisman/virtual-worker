"""MCP client — connect agents to MCP servers via the official ``mcp`` SDK.

Per-request lifecycle: a ``McpManager`` opens the agent's enabled servers inside
an ``async with`` block (sessions stay live so tool calls can run during the
loop) and closes them on exit. Discovered tools are exposed to the loop's
per-request registry as ``mcp__<server-slug>__<tool>``, so no loop changes are
needed (it already dispatches via the registry).

The SDK is optional: if ``mcp`` isn't installed, everything degrades gracefully
(no MCP tools; agents still work).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    _AVAILABLE = True
except Exception:  # noqa: BLE001 — SDK not installed
    _AVAILABLE = False


def mcp_available() -> bool:
    return _AVAILABLE


def _stringify(result) -> str:
    """Flatten a CallToolResult's content blocks into text for the loop."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    out = "\n".join(p for p in parts if p).strip()
    if getattr(result, "isError", False):
        return f"[tool error] {out}" if out else "[tool error]"
    return out or "(no output)"


async def _open_session(stack: AsyncExitStack, cfg: dict):
    transport = cfg.get("transport") or "stdio"
    if transport == "http":
        url = cfg.get("url")
        if not url:
            raise ValueError("http MCP server needs a url")
        read, write, _ = await stack.enter_async_context(streamablehttp_client(url))
    else:
        command = cfg.get("command")
        if not command:
            raise ValueError("stdio MCP server needs a command")
        params = StdioServerParameters(
            command=command, args=cfg.get("args") or [], env=(cfg.get("env") or None),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def _make_fn(session, tool_name: str, slug: str):
    async def fn(**kwargs):
        try:
            result = await session.call_tool(tool_name, kwargs or {})
        except Exception as exc:  # noqa: BLE001
            return f"mcp__{slug}__{tool_name} error: {exc}"
        return _stringify(result)
    return fn


class McpManager:
    """Holds live MCP sessions for one chat request. Use as an async context
    manager; sessions close when the block exits."""

    def __init__(self):
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "McpManager":
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc) -> bool | None:
        return await self._stack.__aexit__(*exc)

    async def build_registry(self, servers: list[dict]) -> dict[str, dict]:
        """Connect each enabled server, list its tools, and return registry
        entries keyed ``mcp__<slug>__<tool>``. A server that fails to connect is
        skipped (logged) — never fatal to the chat."""
        registry: dict[str, dict] = {}
        if not _AVAILABLE:
            if servers:
                logger.warning("MCP servers configured but the 'mcp' SDK is not installed")
            return registry
        for cfg in servers:
            if not cfg or not cfg.get("enabled", True):
                continue
            slug = cfg.get("slug") or "server"
            try:
                session = await _open_session(self._stack, cfg)
                resp = await session.list_tools()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP server %s connect/list failed: %s", slug, exc)
                continue
            for tool in resp.tools:
                qualified = f"mcp__{slug}__{tool.name}"
                registry[qualified] = {
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": qualified,
                            "description": tool.description or f"{tool.name} (via {slug})",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    },
                    "fn": _make_fn(session, tool.name, slug),
                }
        return registry


async def list_server_tools(cfg: dict) -> list[dict]:
    """Connect once, list tools, disconnect. Powers the 'test connection' endpoint."""
    if not _AVAILABLE:
        raise RuntimeError("the 'mcp' Python SDK is not installed")
    async with AsyncExitStack() as stack:
        session = await _open_session(stack, cfg)
        resp = await session.list_tools()
        return [{"name": t.name, "description": t.description or "",
                 "inputSchema": t.inputSchema or {}} for t in resp.tools]
