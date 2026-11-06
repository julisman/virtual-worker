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

import asyncio
import logging

import httpx

from storage import tool_store

logger = logging.getLogger(__name__)

# Cap what we feed back to the model so a chatty endpoint can't blow the context.
MAX_RESULT_CHARS = 8000


def _is_blocked_host(url: str) -> bool:
    """The SSRF guard — delegates to the canonical fail-closed implementation in
    ``agent/tools.py`` so the two can't drift (they used to disagree: this one
    failed closed, that one failed open)."""
    from agent.tools import _is_private_host
    return _is_private_host(url)


def _expand_dotted(flat: dict) -> dict:
    """Expand dotted keys into nested objects so an endpoint expecting a nested
    body can be described with a flat builder. ``{"message.input_str": "hi"}``
    becomes ``{"message": {"input_str": "hi"}}``. A key with no dot is left as-is;
    a path that collides with a non-dict value replaces it with a nested object."""
    out: dict = {}
    for key, val in flat.items():
        if "." not in key:
            out[key] = val
            continue
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = val
    return out


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
    fixed = tool.get("fixedParams") or {}
    allow_private = bool(tool.get("allowPrivate"))
    timeout = int(tool.get("timeout") or 20)

    async def fn(**args) -> str:
        if not url:
            return f"{name} is misconfigured: no URL set."
        target = url if url.startswith(("http://", "https://")) else "https://" + url
        if not allow_private and await asyncio.to_thread(_is_blocked_host, target):
            return (f"{name} refused: that host is private/loopback. Enable "
                    "'allow private hosts' on the tool if this is intentional.")
        # Model args + operator-pinned fixed args -> query params for GET/DELETE,
        # JSON body otherwise. Fixed values win so the model can't override them.
        payload = {**(args or {}), **fixed}
        kwargs: dict = {"headers": headers}
        if method in ("GET", "DELETE"):
            kwargs["params"] = payload  # query strings stay flat
        else:
            kwargs["json"] = _expand_dotted(payload)  # dotted keys -> nested JSON
        try:
            # Follow redirects manually so every hop is re-checked against the
            # SSRF guard — a public URL that 302s to 127.0.0.1 must not slip
            # through by being validated only on the initial target.
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.request(method, target, **kwargs)
                hops = 0
                while resp.is_redirect and hops < 5:
                    nxt = str(resp.next_request.url) if resp.next_request else ""
                    if not nxt or (not allow_private
                                   and await asyncio.to_thread(_is_blocked_host, nxt)):
                        return (f"{name} refused: redirect to a private/loopback host "
                                "was blocked.")
                    resp = await client.send(resp.next_request)
                    hops += 1
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
