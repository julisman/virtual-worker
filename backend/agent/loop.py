"""The agentic loop: stream assistant turns, run tool calls, feed results back.

Uses the same upstream as the rest of the gateway (the local model). Streams
typed events so the UI can show tool activity inline:

  {"type": "text", "delta": "..."}
  {"type": "tool_call", "id", "name", "args"}
  {"type": "tool_result", "id", "name", "result"}
  {"type": "done"} | {"type": "error", "message"}
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from gateway.upstream import UpstreamError, chat_completion_stream
from storage import debug_db
from . import tools as toolkit

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6

# --- Context management ---------------------------------------------------- #
# A long tool loop can pile up huge tool outputs (a 200k-char read_file, web
# pages, shell logs) and overflow the model's context window. Two bounds keep
# the model-facing conversation inside budget without losing the trail (the full
# results still go to the Debug store + the UI):
#   1. cap each tool result that's fed *back into the model*, and
#   2. trim the whole conversation to a char budget before each model call,
#      eliding the oldest bulky turns while protecting the system prompt, the
#      original task, and the most recent turns.
# Budget is in characters (~3.5 chars/token); derive from the model context via
# env, default ~22k tokens of a 32k window.
MAX_TOOL_RESULT_CHARS = int(os.environ.get("VW_MAX_TOOL_RESULT_CHARS", "12000"))
CONTEXT_CHAR_BUDGET = int(os.environ.get("VW_CONTEXT_CHARS", "80000"))
KEEP_RECENT_MSGS = 6


def _cap(text: str, limit: int) -> str:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} chars truncated to fit the model context]"


def _msg_chars(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        n += len(json.dumps(tc))
    return n


def _fit_context(convo: list[dict], budget: int) -> list[dict]:
    """Return a copy of ``convo`` trimmed to ``budget`` chars. Protects the
    system message, the first user message (the task), and the last
    ``KEEP_RECENT_MSGS`` messages; elides the bodies of older tool/assistant
    turns oldest-first until under budget."""
    total = sum(_msg_chars(m) for m in convo)
    if total <= budget:
        return convo
    n = len(convo)
    protected = {i for i, m in enumerate(convo) if m.get("role") == "system"}
    first_user = next((i for i, m in enumerate(convo) if m.get("role") == "user"), None)
    if first_user is not None:
        protected.add(first_user)
    protected |= set(range(max(0, n - KEEP_RECENT_MSGS), n))
    out = [dict(m) for m in convo]
    for i in range(n):
        if total <= budget:
            break
        if i in protected:
            continue
        m = out[i]
        content = m.get("content")
        if isinstance(content, str) and len(content) > 120:
            saved = len(content) - 100
            m["content"] = content[:100] + "\n…[elided to fit the model context]"
            total -= saved
    return out


def registry_from_names(names: list[str]) -> dict[str, dict]:
    """Build a per-request tool registry {name: {schema, fn}} from built-in
    tool names — the default registry when no agent customises it."""
    return {n: toolkit.TOOLS[n] for n in (names or []) if n in toolkit.TOOLS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_text(messages: list[dict], role: str) -> str:
    for m in messages:
        if m.get("role") == role:
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("text"))
    return ""


async def _dispatch(registry: dict[str, dict], name: str, args: dict) -> str:
    """Run a tool from the per-request registry (built-in, load_skill, or MCP)."""
    tool = registry.get(name)
    if not tool:
        return f"unknown tool: {name}"
    try:
        return await tool["fn"](**(args or {}))
    except TypeError as exc:
        return f"bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool %s failed: %s", name, exc)
        return f"{name} error: {exc}"


async def _stream_turn(payload: dict):
    """Stream one assistant turn. Returns (text, tool_calls, finish_reason) and
    yields ('text', delta) tuples for content as it arrives."""
    text = ""
    calls: dict[int, dict] = {}
    finish = None
    usage = None
    async for chunk in chat_completion_stream(payload):
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
                yield ("text", delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    ordered = [calls[i] for i in sorted(calls)]
    yield ("end", {"text": text, "tool_calls": ordered, "finish": finish, "usage": usage})


def _log_activity(agent_id: str, name: str, args: dict, task_id: str | None) -> None:
    """Mirror a tool call into the agent's activity feed (best-effort).

    task_* tools record their own (richer) activity, so we skip them here to
    avoid duplicates. File writes are tagged ``file_change``; the rest ``tool_call``."""
    if not agent_id or name.startswith("task_"):
        return
    try:
        from storage import activity_store
        if name in ("write_file", "edit_file"):
            activity_store.log(agent_id, "file_change", f"{name} {args.get('path', '')}".strip(),
                               task_id=task_id, detail={"tool": name})
        else:
            hint = args.get("query") or args.get("command") or args.get("url") or args.get("path") or ""
            summary = f"{name}" + (f": {str(hint)[:80]}" if hint else "")
            activity_store.log(agent_id, "tool_call", summary, task_id=task_id, detail={"tool": name})
    except Exception:  # noqa: BLE001
        logger.debug("activity log failed", exc_info=True)


async def run(
    messages: list[dict],
    model: str,
    tools: list[str] | dict[str, dict] | None = None,
    temperature: float = 0.7,
    session_id: str = "chat:adhoc",
    system: str | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    max_iterations: int | None = None,
) -> AsyncIterator[dict]:
    """Drive the tool loop, yielding typed event dicts. Records the full call
    trail (prompt → tool calls → answer) to the Debug store under session_id.

    ``tools`` is a per-request registry ``{name: {schema, fn}}`` (the agent's
    composed tools); a bare list of built-in tool names is also accepted for
    backwards compatibility. ``system``, if given, is injected as the leading
    system message (replacing any client-sent one)."""
    registry = registry_from_names(tools) if isinstance(tools, list) or tools is None else dict(tools)
    convo = list(messages)
    if system is not None:
        convo = [m for m in convo if m.get("role") != "system"]
        convo.insert(0, {"role": "system", "content": system})
    tool_schemas = [t["schema"] for t in registry.values()]

    start = _now_iso()
    entries: list[dict] = []
    if (sys := _first_text(convo, "system")):
        entries.append({"type": "system", "text": sys, "timestamp": start})
    entries.append({"type": "user", "text": _first_text(convo, "user"), "timestamp": start})
    final_text = ""
    tool_count = 0
    in_tokens = 0
    out_tokens = 0
    is_error = False
    err_text = None

    try:
        for _ in range(max_iterations or MAX_ITERATIONS):
            # Context management: keep the model-facing conversation within budget.
            convo = _fit_context(convo, CONTEXT_CHAR_BUDGET)
            payload = {"model": model, "messages": convo, "temperature": temperature}
            if top_p is not None:
                payload["top_p"] = top_p
            if top_k is not None:
                payload["top_k"] = top_k
            if tool_schemas:
                payload["tools"] = tool_schemas

            turn = {"text": "", "tool_calls": [], "finish": None, "usage": None}
            async for kind, data in _stream_turn(payload):
                if kind == "text":
                    yield {"type": "text", "delta": data}
                elif kind == "end":
                    turn = data
            final_text += turn["text"]
            usage = turn.get("usage") or {}
            out_tokens += usage.get("completion_tokens", 0) or 0
            if usage.get("prompt_tokens"):
                in_tokens = usage["prompt_tokens"]  # last turn's prompt size

            if not turn["tool_calls"]:
                if turn["text"]:
                    entries.append({"type": "assistant", "text": turn["text"], "timestamp": _now_iso()})
                yield {"type": "done", "outputTokens": out_tokens}
                return

            convo.append({
                "role": "assistant",
                "content": turn["text"] or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in turn["tool_calls"]
                ],
            })

            for c in turn["tool_calls"]:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_count += 1
                entries.append({"type": "tool_call", "tool_name": c["name"],
                                "tool_call_id": c["id"], "tool_args": args, "timestamp": _now_iso()})
                yield {"type": "tool_call", "id": c["id"], "name": c["name"], "args": args}
                _log_activity(agent_id, c["name"], args, task_id)
                result = await _dispatch(registry, c["name"], args)
                entries.append({"type": "tool_result", "tool_name": c["name"],
                                "tool_call_id": c["id"], "tool_result": result, "timestamp": _now_iso()})
                yield {"type": "tool_result", "id": c["id"], "name": c["name"], "result": result[:600]}
                # Feed a capped copy back to the model (full result is in the trail above).
                convo.append({"role": "tool", "tool_call_id": c["id"],
                              "content": _cap(result, MAX_TOOL_RESULT_CHARS)})

        yield {"type": "text", "delta": "\n\n_(stopped: reached max tool iterations)_"}
        yield {"type": "done", "outputTokens": out_tokens}
    except UpstreamError as exc:
        is_error, err_text = True, exc.body[:500]
        yield {"type": "error", "message": exc.body[:500]}
    except Exception as exc:  # noqa: BLE001
        is_error, err_text = True, str(exc)
        logger.warning("agent loop error: %s", exc)
        yield {"type": "error", "message": str(exc)}
    finally:
        _record(session_id, model, start, entries, final_text, tool_count,
                in_tokens, out_tokens, is_error, err_text)


def _record(session_id, model, start, entries, final_text, tool_count,
            in_tokens, out_tokens, is_error, err_text):
    """Persist the loop's trail as one Debug run (best-effort)."""
    try:
        debug_db.insert_run({
            "run_id": str(uuid.uuid4()), "session_id": session_id,
            "start_time": start, "end_time": _now_iso(),
            "method": "POST", "path": "/api/chat", "endpoint": "/api/chat",
            "provider": "local", "model": model, "status_code": 500 if is_error else 200,
            "is_error": is_error, "error_text": err_text, "stream": True,
            "tool_calls": tool_count,
            "input_tokens": in_tokens, "output_tokens": out_tokens,
            "total_tokens": (in_tokens or 0) + (out_tokens or 0),
            "first_user_text": next((e["text"] for e in entries if e["type"] == "user"), None),
            "last_assistant_text": final_text or None,
        }, entries)
    except Exception:
        logger.debug("agent loop: debug record failed", exc_info=True)
