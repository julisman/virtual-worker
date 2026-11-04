"""Custom HTTP tools — the lightweight, first-class alternative to MCP.

``make_custom_tools(tool_ids)`` builds a ``{name: {schema, fn}}`` registry from
custom-tool definitions (``storage/tool_store.py``), mirroring the other per-agent
self-tool factories (``make_memory_tools`` / ``make_schedule_tools`` /
``make_intern_tools``). Each tool's **schema** is its declared JSON Schema and its
**fn** is a generated executor that calls the declared HTTP endpoint with the
model-supplied arguments and returns the response text (truncated).

Safety (PRD-10 §12):
  - **SSRF guard** — refuses loopback / private / link-local / reserved hosts
    unless the tool opts in with ``allowPrivate`` (the operator's explicit choice;
    e.g. for a tool that calls a service on their own machine).
  - **Timeout** — every request is bounded; a slow/failed call returns a structured
    error string, never crashes the loop.
  - **Secrets stay local** — headers (which may carry an API key) live in the
    local store and are sent only on the outbound request, never surfaced to the
    model in the schema or result.

Sandboxed ``script`` (Python) tools are a deferred milestone (PRD-10 §M3).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse

import httpx

from storage import tool_store

logger = logging.getLogger(__name__)

# Cap what we feed back to the model so a chatty endpoint can't blow the context.
MAX_RESULT_CHARS = 8000


def _is_blocked_host(url: str) -> bool:
    """True when ``url``'s host resolves to a loopback/private/link-local/reserved
    address — the SSRF guard. Mirrors ``agent/tools.py``'s web_fetch check, but
    fails closed: an unresolvable host is treated as blocked."""
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


def _truncate(text: str) -> str:
    if text is None:
        return ""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n…(+{len(text) - MAX_RESULT_CHARS} chars truncated)"


def _make_executor(tool: dict):
    """Build the async ``fn`` that performs the HTTP call for one tool def."""
    name = tool["name"]
    method = (tool.get("method") or "GET").upper()
    url = (tool.get("url") or "").strip()
    headers = tool.get("headers") or {}
    allow_private = bool(tool.get("allowPrivate"))
    timeout = int(tool.get("timeout") or 20)

    async def fn(**args) -> str:
        if not url:
            return f"{name} is misconfigured: no URL set."
        target = url if url.startswith(("http://", "https://")) else "https://" + url
        if not allow_private and _is_blocked_host(target):
            return (f"{name} refused: that host is private/loopback. Enable "
                    "'allow private hosts' on the tool if this is intentional.")
        # Model args -> query params for GET/DELETE, JSON body otherwise.
        kwargs: dict = {"headers": headers}
        if method in ("GET", "DELETE"):
            kwargs["params"] = args or {}
        else:
            kwargs["json"] = args or {}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.request(method, target, **kwargs)
        except httpx.TimeoutException:
            return f"{name} error: request timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            return f"{name} error: {exc}"
        body = _truncate(resp.text)
        return f"{name} → HTTP {resp.status_code}\n{body}"

    return fn


def build_schema(tool: dict) -> dict:
    """The OpenAI function schema the model sees for a custom tool."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or f"Custom tool {tool['name']}.",
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def make_custom_tools(tool_ids: list[str]) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` registry for the given custom-tool ids.

    Only **enabled** tools with the ``http`` transport are included; disabled or
    deferred (script) tools are silently skipped so they simply don't appear in
    the agent's advertised toolset this run."""
    registry: dict[str, dict] = {}
    for tool in tool_store.get_tools(tool_ids or []):
        if not tool.get("enabled"):
            continue
        if (tool.get("transport") or "http") != "http":
            continue  # script tools are a deferred milestone
        registry[tool["name"]] = {"schema": build_schema(tool), "fn": _make_executor(tool)}
    return registry


async def test_tool(tool: dict, args: dict | None) -> dict:
    """Invoke a tool def with sample ``args`` (used by the registry Test action).

    Returns a structured result regardless of the tool's enabled/assigned state —
    the operator is explicitly exercising it. Honors the SSRF guard + timeout."""
    fn = _make_executor(tool)
    result = await fn(**(args or {}))
    return {"name": tool["name"], "args": args or {}, "result": result}
