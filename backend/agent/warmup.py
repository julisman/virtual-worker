"""Prefill the local model server's prompt cache right after a model is activated.

An agent's prompt starts with a large fixed part: its tool definitions (often
8-10K tokens) and the start of its system prompt. llama-server keeps processed
prompts in its cache and reuses a matching start on the next request, but right
after a model loads the cache is empty. The first message of every session then
pays for the whole prompt: about 80 s for a 10K-token agent on an M-series Mac.

Warming sends one throwaway request per recently used agent, built exactly like
the first turn of a real run (same ``compose()`` output, same first-turn tool
list, same chat-template kwargs), and asks for a single token. The local chat
templates we ship with render tools first, then the system prompt; the clock
line is the system prompt's last block. So the warmed tokens match every new
session up to that point, and the first real message only processes the rest.

Only the local upstream is warmed (remote providers cache on their side), and
nothing is recorded: the request goes straight to llama-server, not through the
gateway's Debug capture.
"""

from __future__ import annotations

import logging
import time

import httpx

import config
from agent import crusher
from agent.compose import compose
from storage import agent_store, chat_store

logger = logging.getLogger(__name__)

WARMUP_SESSION_ID = "vw-prompt-warmup"
_TIMEOUT_S = 600  # a 10K-token prompt on a slow machine can take minutes


def recent_agent_ids(limit: int) -> list[str]:
    """Agents of the most recently updated chat sessions, newest first, falling
    back to the default agent when there's no history."""
    if limit <= 0:
        return []
    ids: list[str] = []
    try:
        for s in chat_store.list_sessions():  # newest first
            aid = s.get("agentId") or s.get("agent_id")
            if aid and aid not in ids:
                ids.append(aid)
            if len(ids) >= limit:
                break
    except Exception:  # noqa: BLE001 — warming is best-effort
        logger.debug("warmup: could not list sessions", exc_info=True)
    if not ids:
        ids.append(agent_store.DEFAULT_AGENT_ID)
    return ids[:limit]


def build_request(agent: dict) -> dict:
    """The first-turn request a new session of ``agent`` would send, minus the
    user's words: what ``loop.run`` puts on the wire before any message exists.

    Mirrors ``loop.run``'s first turn: the composed system prompt as the first
    message, and every tool in registry order except ``retrieve_full`` (hidden
    until a compression marker exists). MCP tools are not included, because
    connecting servers has side effects; they come after the built-in tools, so
    the warmed part still matches."""
    system, registry, *_ = compose(agent, session_id=WARMUP_SESSION_ID)
    tools = [e["schema"] for n, e in registry.items() if n != crusher.RETRIEVE_TOOL_NAME]
    body: dict = {
        "messages": [{"role": "system", "content": system or ""},
                     {"role": "user", "content": "."}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
        "cache_prompt": True,
    }
    if tools:
        body["tools"] = tools
    g = agent.get("guardrails") or {}
    if g.get("reasoning") == "off":  # loop.run sends this for local models too
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


async def _is_local(agent: dict) -> bool:
    from gateway.providers import resolve_target
    try:
        _, target = await resolve_target(agent, None)
    except Exception:  # noqa: BLE001
        return False
    return target is None


async def warm(port: int, limit: int | None = None) -> list[dict]:
    """Warm the cache of the llama-server on ``port`` for up to ``limit`` agents.
    Returns one result per agent tried; never raises."""
    limit = config.WARMUP_AGENTS if limit is None else limit
    results: list[dict] = []
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    for aid in recent_agent_ids(limit):
        res: dict = {"agentId": aid, "ok": False}
        try:
            agent = agent_store.get_agent(aid)
            if not agent:
                res["skipped"] = "agent not found"
            elif not await _is_local(agent):
                res["skipped"] = "agent uses a remote provider"
            else:
                body = build_request(agent)
                t0 = time.monotonic()
                async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
                    r = await c.post(url, json=body)
                res["seconds"] = round(time.monotonic() - t0, 1)
                if r.status_code == 200:
                    usage = r.json().get("usage") or {}
                    res.update(ok=True, promptTokens=usage.get("prompt_tokens"))
                    logger.info("warmed prompt cache for agent %s: %s tokens in %.1fs",
                                aid, res["promptTokens"], res["seconds"])
                else:
                    res["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:  # noqa: BLE001 — never break model activation
            res["error"] = str(exc)[:200]
        if not res["ok"] and "skipped" not in res:
            logger.info("prompt warm-up for agent %s failed: %s", aid, res.get("error"))
        results.append(res)
    return results
