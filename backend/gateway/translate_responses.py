"""OpenAI Responses API  <->  Chat Completions  (pure translation).

Codex CLI only speaks the Responses API (wire_api="responses"), so this is what
lets `codex` drive a local model. Scope: text + function_call (+ optional
reasoning), stateless — Codex resends full context each turn, so
``previous_response_id``/``store`` are ignored.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

from .ids import fc_id, msg_id, resp_id
from .sse import sse_event


def _now() -> int:
    return int(time.time())


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                out.append(part.get("text", ""))
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return ""


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        return _content_text(value)
    return json.dumps(value)


# --------------------------------------------------------------------------- #
# Request:  Responses -> OpenAI
# --------------------------------------------------------------------------- #

def responses_to_openai(body: dict, model: str) -> dict:
    messages: list[dict] = []

    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            if typ == "function_call":
                call = {"id": item.get("call_id"), "type": "function", "function": {
                    "name": item.get("name"), "arguments": item.get("arguments", "")}}
                # merge into a preceding assistant tool-call message if adjacent
                if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif typ == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item.get("call_id"),
                                 "content": _as_text(item.get("output"))})
            elif typ == "reasoning":
                continue
            else:  # message (explicit or implied by role+content)
                messages.append({"role": item.get("role", "user"),
                                 "content": _content_text(item.get("content"))})

    payload: dict = {"model": model, "messages": messages}
    if body.get("max_output_tokens") is not None:
        payload["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            payload[k] = body[k]
    if body.get("parallel_tool_calls") is not None:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]

    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            if t.get("type") == "function":
                tools.append({"type": "function", "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                }})
        if tools:
            payload["tools"] = tools
        if body.get("tool_choice") is not None:
            payload["tool_choice"] = body["tool_choice"]
    return payload


# --------------------------------------------------------------------------- #
# Response (non-stream):  OpenAI -> Responses
# --------------------------------------------------------------------------- #

def _build_output(msg: dict) -> list[dict]:
    output: list[dict] = []
    if msg.get("reasoning_content"):
        output.append({"type": "reasoning", "id": msg_id(),
                       "summary": [{"type": "summary_text", "text": msg["reasoning_content"]}]})
    if msg.get("content"):
        output.append({"type": "message", "id": msg_id(), "status": "completed",
                       "role": "assistant",
                       "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"type": "function_call", "id": fc_id(), "call_id": tc.get("id"),
                       "name": fn.get("name"), "arguments": fn.get("arguments", ""),
                       "status": "completed"})
    return output


def openai_to_responses(resp: dict, requested_model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason")
    usage = resp.get("usage") or {}

    incomplete = finish == "length"
    return {
        "id": resp_id(),
        "object": "response",
        "created_at": _now(),
        "status": "incomplete" if incomplete else "completed",
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "model": requested_model,
        "output": _build_output(msg),
        "parallel_tool_calls": True,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# --------------------------------------------------------------------------- #
# Streaming:  OpenAI delta stream -> Responses typed events
# --------------------------------------------------------------------------- #

async def stream_responses(
    chunks: AsyncIterator[dict],
    requested_model: str,
) -> AsyncIterator[bytes]:
    seq = 0

    def ev(payload: dict) -> bytes:
        nonlocal seq
        payload["sequence_number"] = seq
        seq += 1
        return sse_event(payload, event=payload["type"])

    response_obj = {
        "id": resp_id(), "object": "response", "created_at": _now(),
        "status": "in_progress", "error": None, "incomplete_details": None,
        "model": requested_model, "output": [], "parallel_tool_calls": True, "usage": None,
    }
    yield ev({"type": "response.created", "response": response_obj})
    yield ev({"type": "response.in_progress", "response": response_obj})

    next_output_index = 0
    msg_state: dict | None = None       # {output_index,item_id,part_added,text,closed}
    tools: dict[int, dict] = {}         # openai tool index -> meta
    final_output: list[dict] = []
    finish_reason = None
    usage = {}

    def close_message():
        nonlocal msg_state
        events = []
        if msg_state and not msg_state.get("closed"):
            oi, item_id, text = msg_state["output_index"], msg_state["item_id"], msg_state["text"]
            if msg_state["part_added"]:
                events.append(ev({"type": "response.output_text.done", "item_id": item_id,
                                  "output_index": oi, "content_index": 0, "text": text}))
                events.append(ev({"type": "response.content_part.done", "item_id": item_id,
                                  "output_index": oi, "content_index": 0,
                                  "part": {"type": "output_text", "text": text, "annotations": []}}))
            item = {"type": "message", "id": item_id, "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            events.append(ev({"type": "response.output_item.done", "output_index": oi, "item": item}))
            final_output.append(item)
            msg_state["closed"] = True
        return events

    async for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            if msg_state is None:
                oi = next_output_index
                next_output_index += 1
                item_id = msg_id()
                msg_state = {"output_index": oi, "item_id": item_id, "part_added": False,
                             "text": "", "closed": False}
                yield ev({"type": "response.output_item.added", "output_index": oi,
                          "item": {"type": "message", "id": item_id, "status": "in_progress",
                                   "role": "assistant", "content": []}})
            if not msg_state["part_added"]:
                yield ev({"type": "response.content_part.added", "item_id": msg_state["item_id"],
                          "output_index": msg_state["output_index"], "content_index": 0,
                          "part": {"type": "output_text", "text": "", "annotations": []}})
                msg_state["part_added"] = True
            msg_state["text"] += text
            yield ev({"type": "response.output_text.delta", "item_id": msg_state["item_id"],
                      "output_index": msg_state["output_index"], "content_index": 0, "delta": text})

        for tc in delta.get("tool_calls") or []:
            t = tc.get("index", 0)
            meta = tools.setdefault(t, {"output_index": None, "item_id": None, "call_id": None,
                                        "name": None, "args": "", "added": False, "buffer": ""})
            if tc.get("id"):
                meta["call_id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                meta["name"] = fn["name"]
            if not meta["added"] and meta["call_id"] and meta["name"]:
                for e in close_message():
                    yield e
                oi = next_output_index
                next_output_index += 1
                meta["output_index"] = oi
                meta["item_id"] = fc_id()
                meta["added"] = True
                yield ev({"type": "response.output_item.added", "output_index": oi,
                          "item": {"type": "function_call", "id": meta["item_id"],
                                   "call_id": meta["call_id"], "name": meta["name"], "arguments": ""}})
                if meta["buffer"]:
                    meta["args"] += meta["buffer"]
                    yield ev({"type": "response.function_call_arguments.delta",
                              "item_id": meta["item_id"], "output_index": oi, "delta": meta["buffer"]})
                    meta["buffer"] = ""
            args = fn.get("arguments")
            if args:
                if meta["added"]:
                    meta["args"] += args
                    yield ev({"type": "response.function_call_arguments.delta",
                              "item_id": meta["item_id"], "output_index": meta["output_index"],
                              "delta": args})
                else:
                    meta["buffer"] += args

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    for e in close_message():
        yield e

    for t in sorted(tools):
        meta = tools[t]
        if not meta["added"]:
            continue
        yield ev({"type": "response.function_call_arguments.done", "item_id": meta["item_id"],
                  "output_index": meta["output_index"], "arguments": meta["args"]})
        item = {"type": "function_call", "id": meta["item_id"], "call_id": meta["call_id"],
                "name": meta["name"], "arguments": meta["args"], "status": "completed"}
        yield ev({"type": "response.output_item.done", "output_index": meta["output_index"], "item": item})
        final_output.append(item)

    incomplete = finish_reason == "length"
    response_obj = {
        **response_obj,
        "status": "incomplete" if incomplete else "completed",
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "output": final_output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    yield ev({"type": "response.completed", "response": response_obj})
