"""Gemini API  <->  OpenAI Chat Completions  (pure translation).

Lets Gemini SDK / Antigravity clients drive a local model via
``/v1beta/models/{model}:generateContent`` (+ ``:streamGenerateContent``).
Gemini's streaming has no named event types — each chunk is the same
``GenerateContentResponse`` shape, partial — so ``gemini_stream_chunks`` yields
native chunk dicts and the route frames them (SSE) or collects them (JSON array).
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .ids import call_id

# finishReason: OpenAI finish_reason -> Gemini
_FINISH_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "function_call": "STOP",
    "content_filter": "SAFETY",
}


def _parts_text(parts: list) -> str:
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)


# --------------------------------------------------------------------------- #
# Request:  Gemini -> OpenAI
# --------------------------------------------------------------------------- #

def gemini_to_openai(body: dict, model: str) -> dict:
    messages: list[dict] = []

    sys = body.get("systemInstruction") or body.get("system_instruction")
    if sys:
        text = _parts_text(sys.get("parts") or [])
        if text:
            messages.append({"role": "system", "content": text})

    # name -> FIFO queue of synthesized tool-call ids, to correlate functionResponse
    pending: dict[str, list[str]] = {}

    for content in body.get("contents") or []:
        role = content.get("role", "user")
        parts = content.get("parts") or []

        if role == "model":
            text_parts, tool_calls = [], []
            for p in parts:
                if "text" in p:
                    text_parts.append(p["text"])
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    cid = call_id()
                    pending.setdefault(fc.get("name", ""), []).append(cid)
                    tool_calls.append({"id": cid, "type": "function", "function": {
                        "name": fc.get("name"), "arguments": json.dumps(fc.get("args") or {})}})
            out: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                out["tool_calls"] = tool_calls
            messages.append(out)
            continue

        # user (or anything else): functionResponse -> tool msgs; text/image -> user msg
        user_parts = []
        for p in parts:
            if "functionResponse" in p:
                fr = p["functionResponse"]
                name = fr.get("name", "")
                ids = pending.get(name) or []
                cid = ids.pop(0) if ids else call_id()
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": json.dumps(fr.get("response") or {})})
            elif "text" in p:
                user_parts.append({"type": "text", "text": p["text"]})
            elif "inlineData" in p or "inline_data" in p:
                data = p.get("inlineData") or p.get("inline_data")
                url = f"data:{data.get('mimeType') or data.get('mime_type','image/png')};base64,{data.get('data','')}"
                user_parts.append({"type": "image_url", "image_url": {"url": url}})
        if user_parts:
            if all(up["type"] == "text" for up in user_parts):
                messages.append({"role": "user", "content": "".join(up["text"] for up in user_parts)})
            else:
                messages.append({"role": "user", "content": user_parts})

    payload: dict = {"model": model, "messages": messages}

    gen = body.get("generationConfig") or body.get("generation_config") or {}
    if gen.get("temperature") is not None:
        payload["temperature"] = gen["temperature"]
    if gen.get("topP") is not None:
        payload["top_p"] = gen["topP"]
    if gen.get("maxOutputTokens") is not None:
        payload["max_tokens"] = gen["maxOutputTokens"]
    if gen.get("stopSequences"):
        payload["stop"] = gen["stopSequences"]
    if gen.get("responseMimeType") == "application/json":
        payload["response_format"] = {"type": "json_object"}

    tools = []
    for t in body.get("tools") or []:
        for fd in t.get("functionDeclarations") or t.get("function_declarations") or []:
            tools.append({"type": "function", "function": {
                "name": fd.get("name"),
                "description": fd.get("description", ""),
                "parameters": fd.get("parameters") or {"type": "object", "properties": {}},
            }})
    if tools:
        payload["tools"] = tools
        mode = (((body.get("toolConfig") or {}).get("functionCallingConfig") or {}).get("mode") or "").upper()
        if mode == "AUTO":
            payload["tool_choice"] = "auto"
        elif mode == "ANY":
            payload["tool_choice"] = "required"
        elif mode == "NONE":
            payload["tool_choice"] = "none"

    return payload


# --------------------------------------------------------------------------- #
# Response (non-stream):  OpenAI -> Gemini
# --------------------------------------------------------------------------- #

def _message_parts(msg: dict) -> list[dict]:
    parts: list[dict] = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
    return parts


def openai_to_gemini(resp: dict, model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = resp.get("usage") or {}
    return {
        "candidates": [{
            "content": {"role": "model", "parts": _message_parts(msg)},
            "finishReason": _FINISH_MAP.get(choice.get("finish_reason"), "STOP"),
            "index": 0,
            "safetyRatings": [],
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": model,
    }


# --------------------------------------------------------------------------- #
# Streaming:  OpenAI delta stream -> Gemini chunk dicts
# --------------------------------------------------------------------------- #

async def gemini_stream_chunks(
    chunks: AsyncIterator[dict],
    model: str,
) -> AsyncIterator[dict]:
    """Yield native Gemini ``GenerateContentResponse`` chunk dicts. Text streams
    live; function calls are buffered and flushed in the final chunk along with
    finishReason + usageMetadata."""
    tools: dict[int, dict] = {}
    finish_reason = None
    usage = {}

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
            yield {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                   "index": 0}]}

        for tc in delta.get("tool_calls") or []:
            t = tc.get("index", 0)
            meta = tools.setdefault(t, {"name": None, "args": ""})
            fn = tc.get("function") or {}
            if fn.get("name"):
                meta["name"] = fn["name"]
            if fn.get("arguments"):
                meta["args"] += fn["arguments"]

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    final_parts = []
    for t in sorted(tools):
        meta = tools[t]
        if not meta["name"]:
            continue
        try:
            args = json.loads(meta["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        final_parts.append({"functionCall": {"name": meta["name"], "args": args}})

    yield {
        "candidates": [{
            "content": {"role": "model", "parts": final_parts},
            "finishReason": _FINISH_MAP.get(finish_reason, "STOP"),
            "index": 0,
            "safetyRatings": [],
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": model,
    }
