"""Anthropic Messages API  <->  OpenAI Chat Completions  (pure translation).

Used by routes/anthropic.py to let Claude Code drive a local OpenAI-compatible
model. Functions here never touch the network — request/response mappers are
plain dict->dict, and the stream mapper is an async generator over already-parsed
OpenAI delta dicts.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .ids import msg_id
from .sse import sse_event

# stop_reason: OpenAI finish_reason -> Anthropic
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _text_from_content(content) -> str:
    """Flatten an Anthropic content value (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _system_text(system) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _user_parts(content: list) -> list:
    """Map user text/image blocks to OpenAI content parts."""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif t == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


# --------------------------------------------------------------------------- #
# Request:  Anthropic -> OpenAI
# --------------------------------------------------------------------------- #

def anthropic_to_openai(body: dict, model: str) -> dict:
    messages: list[dict] = []

    system = _system_text(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []) or []:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant":
            text_parts, tool_calls = [], []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for block in content or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
            out: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                out["tool_calls"] = tool_calls
            messages.append(out)
            continue

        # role == "user" (or anything else treated as user)
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue

        # list content: tool_result blocks -> tool messages; text/image -> user msg
        leftover = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _text_from_content(block.get("content")),
                })
            else:
                leftover.append(block)
        if leftover:
            parts = _user_parts(leftover)
            # collapse to a plain string when it's all text (nicer for upstreams)
            if parts and all(p["type"] == "text" for p in parts):
                messages.append({"role": "user", "content": "".join(p["text"] for p in parts)})
            elif parts:
                messages.append({"role": "user", "content": parts})

    payload: dict = {"model": model, "messages": messages}

    if body.get("max_tokens") is not None:
        payload["max_tokens"] = body["max_tokens"]
    for k_src, k_dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(k_src) is not None:
            payload[k_dst] = body[k_src]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]

    if body.get("tools"):
        payload["tools"] = [
            {"type": "function", "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            }}
            for t in body["tools"]
        ]
        tc = body.get("tool_choice")
        if isinstance(tc, dict):
            kind = tc.get("type")
            if kind == "auto":
                payload["tool_choice"] = "auto"
            elif kind == "any":
                payload["tool_choice"] = "required"
            elif kind == "tool" and tc.get("name"):
                payload["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}

    return payload


# --------------------------------------------------------------------------- #
# Response (non-stream):  OpenAI -> Anthropic
# --------------------------------------------------------------------------- #

def openai_to_anthropic(resp: dict, requested_model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content_blocks: list[dict] = []

    if msg.get("content"):
        content_blocks.append({"type": "text", "text": msg["content"]})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id"),
            "name": fn.get("name"),
            "input": tool_input,
        })

    usage = resp.get("usage") or {}
    return {
        "id": msg_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": _STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --------------------------------------------------------------------------- #
# Token estimate (no tokenizer lib available)
# --------------------------------------------------------------------------- #

def count_tokens_source(body: dict) -> str:
    chunks = [_system_text(body.get("system"))]
    for msg in body.get("messages", []) or []:
        chunks.append(_text_from_content(msg.get("content")))
        for block in (msg.get("content") or []) if isinstance(msg.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                chunks.append(json.dumps(block.get("input") or block.get("content") or ""))
    for tool in body.get("tools", []) or []:
        chunks.append(json.dumps(tool.get("input_schema") or {}))
    return "\n".join(c for c in chunks if c)


def estimate_tokens(text: str) -> int:
    """~4 chars/token. Approximate — Claude Code uses count_tokens for the
    context-budget display only."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Streaming:  OpenAI delta stream -> Anthropic SSE events
# --------------------------------------------------------------------------- #

async def stream_anthropic(
    chunks: AsyncIterator[dict],
    requested_model: str,
    input_tokens: int,
) -> AsyncIterator[bytes]:
    """Translate an OpenAI streaming completion into the Anthropic SSE event
    sequence. One content block is open at a time (local models emit blocks
    sequentially), tracked by a flat index machine."""
    message = {
        "id": msg_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }
    yield sse_event({"type": "message_start", "message": message}, event="message_start")
    yield sse_event({"type": "ping"}, event="ping")

    next_index = 0
    open_block: tuple | None = None        # ("text", idx) | ("tool", t, idx)
    tools: dict[int, dict] = {}            # openai tool index -> meta
    finish_reason = None
    output_tokens = 0

    def close_open():
        nonlocal open_block
        if open_block is not None:
            ev = sse_event(
                {"type": "content_block_stop", "index": open_block[-1]},
                event="content_block_stop",
            )
            open_block = None
            return ev
        return None

    async for chunk in chunks:
        u = chunk.get("usage")
        if u:
            output_tokens = u.get("completion_tokens", output_tokens) or output_tokens

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            if open_block is None or open_block[0] != "text":
                ev = close_open()
                if ev:
                    yield ev
                idx = next_index
                next_index += 1
                open_block = ("text", idx)
                yield sse_event({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""},
                }, event="content_block_start")
            yield sse_event({
                "type": "content_block_delta", "index": open_block[-1],
                "delta": {"type": "text_delta", "text": text},
            }, event="content_block_delta")

        for tc in delta.get("tool_calls") or []:
            t = tc.get("index", 0)
            meta = tools.setdefault(t, {"index": None, "id": None, "name": None,
                                        "started": False, "buffer": ""})
            if tc.get("id"):
                meta["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                meta["name"] = fn["name"]

            if not meta["started"] and meta["id"] and meta["name"]:
                ev = close_open()
                if ev:
                    yield ev
                idx = next_index
                next_index += 1
                meta["index"] = idx
                meta["started"] = True
                open_block = ("tool", t, idx)
                yield sse_event({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use", "id": meta["id"],
                                      "name": meta["name"], "input": {}},
                }, event="content_block_start")
                if meta["buffer"]:
                    yield sse_event({
                        "type": "content_block_delta", "index": idx,
                        "delta": {"type": "input_json_delta", "partial_json": meta["buffer"]},
                    }, event="content_block_delta")
                    meta["buffer"] = ""

            args = fn.get("arguments")
            if args:
                if meta["started"]:
                    if open_block != ("tool", t, meta["index"]):
                        ev = close_open()
                        if ev:
                            yield ev
                        open_block = ("tool", t, meta["index"])
                    yield sse_event({
                        "type": "content_block_delta", "index": meta["index"],
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }, event="content_block_delta")
                else:
                    meta["buffer"] += args

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    ev = close_open()
    if ev:
        yield ev
    yield sse_event({
        "type": "message_delta",
        "delta": {"stop_reason": _STOP_MAP.get(finish_reason, "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }, event="message_delta")
    yield sse_event({"type": "message_stop"}, event="message_stop")
