"""Egress to external LLM providers — the opt-in remote counterpart of upstream.py.

``upstream.py`` talks to the *local* model server. This module talks to *remote*
paid providers when an agent is explicitly switched off "local" (see
``gateway/providers.py``). It exposes one entry point, :func:`stream`, that mirrors
``upstream.chat_completion_stream``: it takes the OpenAI-shaped ``payload`` the
agent loop already builds and yields OpenAI-style streaming **chunk dicts**
(``{"choices":[{"delta":{...}}], "usage":{...}}``). The loop's ``_stream_turn``
therefore needs no changes — every provider is normalized back to the OpenAI wire
shape here.

Targets (from ``providers.resolve_target``) carry ``kind``:
  - ``openai``    — OpenAI-compatible Chat Completions (DeepSeek, OpenAI). Pure
                    passthrough with a Bearer auth header.
  - ``anthropic`` — Anthropic Messages API. The request is translated to the
                    Anthropic shape and the SSE response translated back.
  - ``responses`` — OpenAI-compatible Responses API. The request and stream are
                    translated to/from the agent loop's chat-completions shape.

``target is None`` falls through to the local upstream, so callers can route
every turn through :func:`stream` regardless of where it runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncIterator

import httpx

from gateway.upstream import UpstreamError, chat_completion_stream as _local_stream

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=180.0, pool=15.0)
# Extra reconnect attempts when a provider stream fails to connect *before* it
# emits anything — a transient network blip ("All connection attempts failed")
# shouldn't abort a long multi-tool agent turn. Once a chunk has streamed we
# can't safely restart, so a later failure surfaces as usual.
_CONNECT_RETRIES = 2

# Sampling/format params the agent loop may attach that aren't valid on the
# OpenAI-compatible Chat Completions API (llama.cpp accepts top_k; OpenAI/DeepSeek
# reject it). Stripped before an external openai-kind call.
_OPENAI_DROP = {"top_k", "prompt_cache_key", "prompt_cache_retention",
                "_reasoning_sidecars", "_reasoning_replay_plain_input"}
_CUSTOM_PROVIDER = "openai-compatible"
_OPENCODE_GO_PROVIDER = "opencode-go"


def _has_reasoning_text(item: dict) -> bool:
    """A reasoning item carrying non-empty plaintext reasoning_text content."""
    for c in item.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "reasoning_text":
            text = c.get("text")
            if isinstance(text, str) and text.strip():
                return True
    return False

_ANTHROPIC_VERSION = "2023-06-01"
# Beta headers + system prefix a Claude subscription (OAuth) token expects — the
# exact string the Claude Code client sends. The OAuth Messages API silently 400s
# unless the system prompt begins with the Claude Code prefix below.
_ANTHROPIC_OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
_CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


async def stream(payload: dict, target: dict | None) -> AsyncIterator[dict]:
    """Stream one turn, yielding OpenAI-style chunk dicts. ``target is None``
    routes to the local upstream (unchanged behavior)."""
    if not target:
        async for chunk in _local_stream(payload):
            yield chunk
        return
    kind = target.get("kind")
    if not target.get("base_url"):
        raise UpstreamError(400, "No base URL configured for the external provider.")
    no_auth = (target.get("provider") == _CUSTOM_PROVIDER
               and target.get("auth_mode") == "none")
    if not target.get("api_key") and not no_auth:
        if kind == "codex":
            raise UpstreamError(
                400, "Not signed in to ChatGPT. Connect it under Providers → OpenAI "
                     "(subscription), or switch this agent back to the local active model.")
        provider = target.get("provider", "external")
        mode = "subscription token" if target.get("auth_mode") == "subscription" else "API key"
        raise UpstreamError(
            400, f"No {mode} configured for {provider}. Add it under Providers, "
                 f"or switch this agent back to the local active model.")
    if kind == "anthropic":
        label = target.get("provider", "anthropic")
        stream_fn = lambda: _anthropic_stream(payload, target)  # noqa: E731
    elif kind == "codex":
        label = "ChatGPT (Codex)"
        stream_fn = lambda: _codex_stream(payload, target)  # noqa: E731
    elif kind == "responses":
        label = target.get("provider", "external")
        stream_fn = lambda: _responses_stream(payload, target)  # noqa: E731
    else:
        label = target.get("provider", "external")
        stream_fn = lambda: _openai_stream(payload, target)  # noqa: E731
    async for chunk in _with_connect_retry(stream_fn, label):
        yield chunk


async def _with_connect_retry(make_stream, provider: str) -> AsyncIterator[dict]:
    """Wrap a provider stream: if it raises a network error *before yielding any
    chunk* — a transient connect blip — reconnect and retry with a short backoff.
    Once a chunk has streamed, restarting would duplicate output, so a later
    failure is surfaced as ``<provider> unreachable`` instead."""
    for attempt in range(_CONNECT_RETRIES + 1):
        yielded = False
        try:
            async for chunk in make_stream():
                yielded = True
                yield chunk
            return
        except httpx.HTTPError as exc:
            if yielded or attempt == _CONNECT_RETRIES:
                raise UpstreamError(502, f"{provider} unreachable: {exc}") from exc
            logger.warning("%s connect blip (attempt %d/%d), retrying: %s",
                           provider, attempt + 1, _CONNECT_RETRIES + 1, exc)
            await asyncio.sleep(0.4 * (2 ** attempt))


# --------------------------------------------------------------------------- #
# SSE helper
# --------------------------------------------------------------------------- #

def _iter_data(line: str) -> str | None:
    """Return the JSON payload of an SSE ``data:`` line, or None for other lines."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    return line[len("data:"):].strip()


# --------------------------------------------------------------------------- #
# OpenAI-compatible (DeepSeek, OpenAI, any compatible base URL)
# --------------------------------------------------------------------------- #

def _openai_body(payload: dict, target: dict) -> dict:
    """Build the OpenAI Chat Completions body from the loop's payload.

    Drops params the API rejects (``top_k``). ``reasoning_effort`` is an
    OpenAI extension rather than part of the OpenAI-compatible contract, so it
    is forwarded only to OpenAI itself. Other compatible gateways retain their
    sampling parameters rather than receiving an unsupported field.

    A custom gateway may declare a ``thinking_format`` dialect instead: for
    ``"openai"`` uses the top-level ``reasoning_effort`` extension when the
    custom gateway has opted into forwarding it. For ``"openrouter"`` the
    reasoning knob rides in a ``reasoning`` object
    (``{"effort": …}`` dialed, including ``{"effort": "none"}`` on explicit ``"off"``
    level) rather than the bare ``reasoning_effort``. For ``"zai"`` (Z.ai-native,
    e.g. GLM) it rides in a ``thinking`` object (``{"type": "enabled"|"disabled"}``)
    with NO effort param — that model's dial doesn't work through the gateway, so
    only on/off is ever sent. An UNSET effort sends nothing in either dialect:
    "Model default" means the model's native behavior, untouched.
    """
    body = {k: v for k, v in payload.items() if k not in _OPENAI_DROP}
    effort = body.pop("reasoning_effort", None)
    thinking_format = target.get("thinking_format")
    if thinking_format == "openrouter":
        if effort == "off":
            body["reasoning"] = {"effort": "none"}
        elif effort:
            body["reasoning"] = {"effort": effort}
    elif thinking_format == "zai":
        if effort == "off":
            body["thinking"] = {"type": "disabled"}
        elif effort:
            body["thinking"] = {"type": "enabled"}
    elif effort and effort != "off" and (
            target.get("provider") == "openai"
            or (target.get("provider") == "openai-compatible"
                and thinking_format in (None, "openai")
                and target.get("reasoning_effort"))):
        body["reasoning_effort"] = effort
        body.pop("temperature", None)
        body.pop("top_p", None)
    if target.get("prompt_cache_key") and payload.get("prompt_cache_key"):
        body["prompt_cache_key"] = payload["prompt_cache_key"]
    if target.get("prompt_cache_retention"):
        body["prompt_cache_retention"] = target["prompt_cache_retention"]
    return body


async def _openai_stream(payload: dict, target: dict) -> AsyncIterator[dict]:
    body = _openai_body(payload, target)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True, **(body.get("stream_options") or {})}
    url = _endpoint_url(target["base_url"], "chat/completions")
    headers = {"Content-Type": "application/json", "accept": "text/event-stream"}
    if target.get("api_key"):
        headers["Authorization"] = f"Bearer {target['api_key']}"
    retried_cache_fields = False
    while True:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code // 100 != 2:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    cache_fields = ("prompt_cache_key", "prompt_cache_retention")
                    if (not retried_cache_fields
                            and any(field in body and field in text for field in cache_fields)):
                        body.pop("prompt_cache_key", None)
                        body.pop("prompt_cache_retention", None)
                        retried_cache_fields = True
                        continue
                    raise UpstreamError(resp.status_code, text)
                async for line in resp.aiter_lines():
                    data = _iter_data(line)
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        raw = dict(usage)
                        details = raw.get("prompt_tokens_details") or {}
                        cache_read = (details.get("cached_tokens")
                                      or raw.get("prompt_cache_hit_tokens")
                                      or raw.get("cached_tokens") or 0)
                        cache_write = (raw.get("cache_write_tokens")
                                       or raw.get("prompt_cache_write_tokens")
                                       or raw.get("cache_creation_input_tokens") or 0)
                        normalized = dict(raw)
                        normalized.update({"cacheRead": int(cache_read or 0),
                                           "cacheWrite": int(cache_write or 0),
                                           "raw": raw})
                        chunk["usage"] = normalized
                    yield chunk
        finally:
            # A pre-stream connect error propagates raw to _with_connect_retry (which
            # may retry it); anything else surfaces unchanged.
            await client.aclose()
        return


# --------------------------------------------------------------------------- #
# Anthropic Messages API (translated to/from the OpenAI shape)
# --------------------------------------------------------------------------- #

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _image_block(url: str) -> dict:
    """Map an OpenAI ``image_url`` (data: URL) to an Anthropic image block."""
    if url.startswith("data:"):
        try:
            head, b64 = url.split(",", 1)
            media_type = head[len("data:"):].split(";", 1)[0] or "image/png"
            base64.b64decode(b64[:8] + "==")  # cheap validity probe
            return {"type": "image", "source": {"type": "base64",
                                                "media_type": media_type, "data": b64}}
        except Exception:  # noqa: BLE001
            pass
    return {"type": "image", "source": {"type": "url", "url": url}}


# Newer Anthropic models removed the sampling params — sending temperature/top_p/
# top_k returns a 400 ("`temperature` is deprecated for this model."). Matched by
# id substring so both bare ("claude-opus-4-8") and prefixed ids are caught.
_ANTHROPIC_NO_SAMPLING = ("opus-4-8", "opus-4-7", "fable-5", "mythos-5")


def _anthropic_rejects_sampling(model: str | None) -> bool:
    m = (model or "").lower()
    return any(tag in m for tag in _ANTHROPIC_NO_SAMPLING)


# Anthropic model ids that accept ``output_config.effort`` (verified against live
# Anthropic docs 2026-08: no beta header, values low/medium/high/xhigh/max).
# Older models 400 on unknown params, so effort is dropped for anything not here.
_ANTHROPIC_EFFORT_PREFIXES = (
    "claude-fable-5", "claude-mythos", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-4-6",
)


def _anthropic_supports_effort(model: str | None) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _ANTHROPIC_EFFORT_PREFIXES)


def _cache_marker(target: dict) -> dict:
    marker = {"type": "ephemeral"}
    if target.get("cache_ttl") == "1h":
        marker["ttl"] = "1h"
    return marker


def _to_anthropic_request(payload: dict, target: dict) -> dict:
    """Translate an OpenAI Chat Completions payload to an Anthropic Messages body."""
    system_parts: list[str] = []
    messages: list[dict] = []
    for m in payload.get("messages") or []:
        role = m.get("role")
        if role == "system":
            if (t := _text_of(m.get("content"))):
                system_parts.append(t)
        elif role == "tool":
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                 "content": str(m.get("content") or "")}]})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"),
                               "name": fn.get("name"), "input": args})
            messages.append({"role": "assistant", "content": blocks or ""})
        else:  # user
            content = m.get("content")
            if isinstance(content, list):
                blocks = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        blocks.append({"type": "text", "text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        blocks.append(_image_block((p.get("image_url") or {}).get("url", "")))
                messages.append({"role": "user", "content": blocks})
            else:
                messages.append({"role": "user", "content": content or ""})

    req: dict = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": True,
        "max_tokens": int(payload.get("max_tokens") or 4096),
    }
    joined = "\n\n".join(s for s in system_parts if s)
    cache_control = bool(target.get("cache_control"))
    if target.get("auth_mode") == "subscription":
        # Claude subscription (OAuth) tokens require the *first* system block to be
        # exactly the Claude Code preamble — sent as its own array element, NOT
        # concatenated into the string (a merged string fails Anthropic's OAuth
        # check and comes back as a misleading rate_limit_error). The rest of the
        # composed system follows as a second block.
        blocks = [{"type": "text", "text": _CLAUDE_CODE_PREFIX}]
        if joined:
            if cache_control and "\n\n" in joined:
                stable, clock = joined.rsplit("\n\n", 1)
                blocks.extend([
                    {"type": "text", "text": stable,
                     "cache_control": _cache_marker(target)},
                    {"type": "text", "text": clock},
                ])
            else:
                block = {"type": "text", "text": joined}
                if cache_control:
                    block["cache_control"] = _cache_marker(target)
                blocks.append(block)
        req["system"] = blocks
    elif joined:
        if cache_control and "\n\n" in joined:
            stable, clock = joined.rsplit("\n\n", 1)
            req["system"] = [
                {"type": "text", "text": stable,
                 "cache_control": _cache_marker(target)},
                {"type": "text", "text": clock},
            ]
        elif cache_control:
            req["system"] = [{"type": "text", "text": joined,
                               "cache_control": _cache_marker(target)}]
        else:
            req["system"] = joined
    if cache_control and messages:
        final = messages[-1]
        content = final.get("content")
        marker = _cache_marker(target)
        if isinstance(content, str):
            final["content"] = [{"type": "text", "text": content,
                                  "cache_control": marker}]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = marker
    # Reasoning effort steers the model instead of sampling — translated to
    # ``output_config.effort`` on models that support it (older ids 400 on the
    # unknown param, so it's silently dropped there). When applied, sampling
    # params are dropped: effort is the steering control.
    effort = payload.pop("reasoning_effort", None)
    apply_effort = bool(effort) and _anthropic_supports_effort(payload.get("model"))
    if apply_effort:
        req["output_config"] = {"effort": effort}
    # Sampling params: newer models (Opus 4.7/4.8, Fable 5) reject temperature/top_p
    # outright (400); the rest accept at most ONE of temperature or top_p, never both.
    if not apply_effort and not _anthropic_rejects_sampling(payload.get("model")):
        if payload.get("temperature") is not None:
            req["temperature"] = payload["temperature"]
        elif payload.get("top_p") is not None:
            req["top_p"] = payload["top_p"]
    tools = []
    for t in payload.get("tools") or []:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        tools.append({"name": fn.get("name"), "description": fn.get("description", ""),
                      "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        req["tools"] = tools
    return req


def _anthropic_headers(target: dict) -> dict:
    headers = {"anthropic-version": _ANTHROPIC_VERSION, "content-type": "application/json"}
    if target.get("auth_mode") == "subscription":
        headers["Authorization"] = f"Bearer {target['api_key']}"
        headers["anthropic-beta"] = _ANTHROPIC_OAUTH_BETA
    else:
        headers["x-api-key"] = target["api_key"]
    return headers


def _chunk_text(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def _chunk_tool_start(index: int, call_id: str, name: str) -> dict:
    return {"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
        {"index": index, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": ""}}]}}]}


def _chunk_tool_args(index: int, fragment: str) -> dict:
    return {"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
        {"index": index, "function": {"arguments": fragment}}]}}]}


def _finish_reason(stop: str | None) -> str:
    return {"tool_use": "tool_calls", "max_tokens": "length",
            "end_turn": "stop", "stop_sequence": "stop"}.get(stop or "", "stop")


async def _anthropic_stream(payload: dict, target: dict) -> AsyncIterator[dict]:
    req = _to_anthropic_request(payload, target)
    # OpenCode Go's Messages endpoint is rooted at its already-versioned API
    # base; Anthropic's native endpoint is rooted one segment above ``/v1``.
    url = (_endpoint_url(target["base_url"], "messages")
           if target.get("provider") == _OPENCODE_GO_PROVIDER
           else f"{target['base_url'].rstrip('/')}/v1/messages")
    headers = _anthropic_headers(target)
    tool_index = -1  # OpenAI tool_calls index, incremented per tool_use block
    prompt_tokens = 0
    cache_read = 0
    cache_write = 0
    raw_usage: dict = {}
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        async with client.stream("POST", url, json=req, headers=headers) as resp:
            if resp.status_code // 100 != 2:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise UpstreamError(resp.status_code, text)
            async for line in resp.aiter_lines():
                data = _iter_data(line)
                if not data:
                    continue
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "message_start":
                    usage = (ev.get("message") or {}).get("usage") or {}
                    raw_usage.update(usage)
                    prompt_tokens = usage.get("input_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
                elif etype == "content_block_start":
                    blk = ev.get("content_block") or {}
                    if blk.get("type") == "tool_use":
                        tool_index += 1
                        yield _chunk_tool_start(tool_index, blk.get("id"), blk.get("name"))
                elif etype == "content_block_delta":
                    d = ev.get("delta") or {}
                    dtype = d.get("type")
                    if dtype == "text_delta" and d.get("text"):
                        yield _chunk_text(d["text"])
                    elif dtype == "input_json_delta":
                        yield _chunk_tool_args(tool_index, d.get("partial_json", ""))
                elif etype == "message_delta":
                    stop = (ev.get("delta") or {}).get("stop_reason")
                    usage = ev.get("usage") or {}
                    raw_usage.update(usage)
                    cache_read = usage.get("cache_read_input_tokens", cache_read) or 0
                    cache_write = (usage.get("cache_creation_input_tokens", cache_write)
                                   or cache_write)
                    out_tokens = usage.get("output_tokens", 0) or 0
                    if stop:
                        prompt_total = prompt_tokens + cache_read + cache_write
                        yield {"choices": [{"index": 0, "delta": {},
                                            "finish_reason": _finish_reason(stop)}],
                               "usage": {"prompt_tokens": prompt_total,
                                         "completion_tokens": out_tokens,
                                         "total_tokens": prompt_total + out_tokens,
                                         "cacheRead": int(cache_read),
                                         "cacheWrite": int(cache_write),
                                         "raw": dict(raw_usage)}}
                elif etype == "message_stop":
                    break
                elif etype == "error":
                    raise UpstreamError(502, json.dumps(ev.get("error") or ev))
    finally:
        # A pre-stream connect error propagates raw to _with_connect_retry (which
        # may retry it); anything else surfaces unchanged.
        await client.aclose()


# --------------------------------------------------------------------------- #
# OpenAI-compatible Responses API + ChatGPT subscription Codex API
# --------------------------------------------------------------------------- #
# The ChatGPT login token can only call the Codex backend, which speaks the
# Responses API (not Chat Completions). We translate the OpenAI request into
# Responses ``input`` items and translate the Responses SSE back to OpenAI chunks.
# Prior tool calls/results are flattened into text (the Codex history shape),
# mirroring a known-good client.

_CODEX_ORIGINATOR = "codex_cli_rs"


def _endpoint_url(base_url: str, path: str) -> str:
    """Append an API path to a base URL, while also accepting a full endpoint."""
    base = base_url.rstrip("/")
    suffix = f"/{path}"
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _codex_user_content(content) -> list[dict]:
    if isinstance(content, list):
        parts: list[dict] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                parts.append({"type": "input_text", "text": p.get("text", "")})
            elif p.get("type") == "image_url":
                parts.append({"type": "input_image",
                              "image_url": (p.get("image_url") or {}).get("url", ""),
                              "detail": "auto"})
        return parts or [{"type": "input_text", "text": ""}]
    return [{"type": "input_text", "text": content or ""}]


def _to_responses_request(payload: dict, target: dict | None = None) -> dict:
    sidecars = payload.pop("_reasoning_sidecars", None) or {}
    instructions: list[str] = []
    items: list[dict] = []
    if not sidecars:
        for m in payload.get("messages") or []:
            role = m.get("role")
            if role == "system":
                if (t := _text_of(m.get("content"))):
                    instructions.append(t)
            elif role == "tool":
                # Native Responses tool-result item (paired with the function_call below
                # by call_id). Flattening this to text teaches the model to emit its own
                # tool calls as text, which silently breaks the multi-turn tool loop.
                items.append({"type": "function_call_output",
                              "call_id": m.get("tool_call_id") or "",
                              "output": str(m.get("content") or "")})
            elif role == "assistant":
                if m.get("content"):
                    items.append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": str(m["content"])}]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    items.append({"type": "function_call",
                                  "call_id": tc.get("id") or "",
                                  "name": fn.get("name"),
                                  "arguments": fn.get("arguments") or "{}"})
            else:  # user
                items.append({"type": "message", "role": "user",
                              "content": _codex_user_content(m.get("content"))})
    else:
        from storage import reasoning_store

        items_plain: list[dict] = []
        replayed = False
        body_model = payload.get("model")
        for i, m in enumerate(payload.get("messages") or []):
            role = m.get("role")
            if role == "system":
                if (t := _text_of(m.get("content"))):
                    instructions.append(t)
                continue
            if role == "tool":
                normal = [{"type": "function_call_output",
                           "call_id": m.get("tool_call_id") or "",
                           "output": str(m.get("content") or "")}]
            elif role == "assistant":
                normal = []
                if m.get("content"):
                    normal.append({"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": str(m["content"])}]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    normal.append({"type": "function_call",
                                   "call_id": tc.get("id") or "",
                                   "name": fn.get("name"),
                                   "arguments": fn.get("arguments") or "{}"})
            else:  # user
                normal = [{"type": "message", "role": "user",
                           "content": _codex_user_content(m.get("content"))}]

            items_plain.extend(normal)
            if role != "assistant":
                items.extend(normal)
                continue

            row = sidecars.get(i)
            replay_ok = False
            mismatch = None
            try:
                if not row:
                    mismatch = "no-sidecar"
                elif not (isinstance(row.get("items"), list)
                          and all(isinstance(x, dict) for x in row["items"])):
                    mismatch = "bad-items"
                elif reasoning_store.sidecar_fingerprint(m) != row.get("fingerprint"):
                    mismatch = "fingerprint"
                elif (target or {}).get("provider") != row.get("provider"):
                    mismatch = "provider"
                elif body_model != row.get("model"):
                    mismatch = "model"
                elif (((target or {}).get("account_id")
                       or (target or {}).get("base_url") or "")
                      != (row.get("originId") or "")):
                    mismatch = "origin"
                else:
                    replay_ok = True
            except Exception:
                mismatch = "exception"
            if replay_ok:
                # Sidecars are persisted with the plaintext ``summary`` stripped,
                # but the Responses API requires the field on reasoning input
                # items — restore it as empty.
                items.extend({**x, "summary": []}
                             if x.get("type") == "reasoning" and "summary" not in x
                             else x for x in row["items"])
                replayed = True
                # Counts/seq/model only — NEVER reasoning content.
                logger.debug("reasoning replay engaged seq=%d items=%d model=%s",
                             i, len(row["items"]), body_model)
            else:
                if row is not None:
                    logger.debug("reasoning replay mismatch seq=%d reason=%s model=%s",
                                 i, mismatch, body_model)
                items.extend(normal)

    body: dict = {
        "model": payload.get("model"),
        "instructions": "\n\n".join(instructions) or "You are a helpful assistant.",
        "input": items,
        "stream": True,
    }
    if sidecars and replayed:
        body["_reasoning_replay_plain_input"] = items_plain
    tools = []
    for t in payload.get("tools") or []:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        # Responses uses the *flat* function-tool shape (name/description/parameters).
        tools.append({"type": "function", "name": fn.get("name"),
                      "description": fn.get("description", ""),
                      "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    if payload.get("max_tokens") is not None:
        body["max_output_tokens"] = payload["max_tokens"]
    # Reasoning effort steers the model instead of sampling: when set, drop
    # temperature/top_p (the Responses API rejects the combination).
    effort = payload.get("reasoning_effort")
    if effort:
        body["reasoning"] = {"effort": "none" if effort == "off" else effort}
    else:
        for key in ("temperature", "top_p"):
            if payload.get(key) is not None:
                body[key] = payload[key]
    if target and target.get("prompt_cache_key") and payload.get("prompt_cache_key"):
        body["prompt_cache_key"] = payload["prompt_cache_key"]
    if target and target.get("prompt_cache_retention"):
        body["prompt_cache_retention"] = target["prompt_cache_retention"]
    return body


def _to_codex_request(payload: dict, target: dict | None = None) -> dict:
    # Read the effort non-mutatingly; _to_responses_request also reads it without popping.
    effort = payload.get("reasoning_effort") or "medium"
    if effort == "off":
        effort = "none"
    body = _to_responses_request(payload, target)
    # The Codex backend gates the allowed-model set on a genuine-client request
    # shape. Keep these subscription-specific fields out of generic providers.
    body.update({
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": effort, "summary": "auto"},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": "medium"},
    })
    for key in ("max_output_tokens", "temperature", "top_p"):
        body.pop(key, None)
    if payload.get("prompt_cache_key"):
        body["prompt_cache_key"] = payload["prompt_cache_key"]
    if target and target.get("prompt_cache_retention"):
        body["prompt_cache_retention"] = target["prompt_cache_retention"]
    return body


def _responses_headers(target: dict) -> dict:
    headers = {"Content-Type": "application/json", "accept": "text/event-stream"}
    if target.get("api_key"):
        headers["Authorization"] = f"Bearer {target['api_key']}"
    return headers


def _codex_headers(target: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {target['api_key']}",
        "accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": _CODEX_ORIGINATOR,
    }
    if target.get("account_id"):
        headers["chatgpt-account-id"] = target["account_id"]
    return headers


def _extract_output_text(item: dict) -> str:
    return "".join(b.get("text", "") for b in (item.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "output_text")


async def _codex_stream(payload: dict, target: dict) -> AsyncIterator[dict]:
    req = _to_codex_request(payload, target)
    url = target.get("base_url") or "https://chatgpt.com/backend-api/codex/responses"
    headers = _codex_headers(target)
    async for chunk in _responses_request_stream(req, url, headers, target):
        yield chunk


async def _responses_stream(payload: dict, target: dict) -> AsyncIterator[dict]:
    req = _to_responses_request(payload, target)
    url = _endpoint_url(target["base_url"], "responses")
    headers = _responses_headers(target)
    async for chunk in _responses_request_stream(req, url, headers, target):
        yield chunk


async def _responses_request_stream(req: dict, url: str,
                                    headers: dict, target: dict | None = None) -> AsyncIterator[dict]:
    plain_input = req.pop("_reasoning_replay_plain_input", None)
    retried_cache_fields = False
    retried_reasoning = False
    while True:
        tool_index = -1
        have_tools = False
        saw_text = False
        captured_meta: list[tuple[int | None, int, dict]] = []
        reasoning_by_id: dict[str, dict] = {}
        arrival = 0
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            async with client.stream("POST", url, json=req, headers=headers) as resp:
                if resp.status_code // 100 != 2:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    cache_fields = ("prompt_cache_key", "prompt_cache_retention")
                    if (not retried_cache_fields
                            and any(field in req and field in text for field in cache_fields)):
                        req.pop("prompt_cache_key", None)
                        req.pop("prompt_cache_retention", None)
                        retried_cache_fields = True
                        continue
                    if (not retried_reasoning and plain_input is not None
                            and any(token in text.lower()
                                    for token in ("encrypted_content", "reasoning",
                                                  "encrypted", "summary"))):
                        req["input"] = plain_input
                        retried_reasoning = True
                        continue
                    raise UpstreamError(resp.status_code, text)
                async for line in resp.aiter_lines():
                    data = _iter_data(line)
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype == "response.output_text.delta":
                        delta = ev.get("delta")
                        if delta:
                            saw_text = True
                            yield _chunk_text(delta)
                    elif etype == "response.reasoning_text.delta":
                        # Surface reasoning in the side channel the loop already
                        # reads — dropping it made a reasoning-heavy turn look
                        # like a completely silent stream in the Debug trace.
                        if ev.get("delta"):
                            yield {"choices": [{"index": 0, "finish_reason": None,
                                                "delta": {"reasoning_content": ev["delta"]}}]}
                    elif etype == "response.output_item.done":
                        item = ev.get("item") or {}
                        itype = item.get("type")
                        if itype in ("reasoning", "message", "function_call"):
                            output_index = item.get("output_index", ev.get("output_index"))
                            if itype == "reasoning":
                                # Keep reasoning candidates until completion: a provider may
                                # omit encrypted_content in the done event and backfill it from
                                # response.output below. Items that remain unencrypted are
                                # discarded before the terminal chunk is emitted.
                                item_id = item.get("id")
                                existing = reasoning_by_id.get(item_id) if item_id else None
                                if existing is not None:
                                    parts = item.get("summary") or []
                                    if parts:
                                        summary = existing.get("summary")
                                        if not isinstance(summary, list):
                                            existing["summary"] = list(parts)
                                        else:
                                            summary.extend(parts)
                                    if (not existing.get("encrypted_content")
                                            and item.get("encrypted_content")):
                                        existing["encrypted_content"] = item["encrypted_content"]
                                else:
                                    captured_meta.append((output_index, arrival, item))
                                    if item_id:
                                        reasoning_by_id[item_id] = item
                            else:
                                captured_meta.append((output_index, arrival, item))
                            arrival += 1
                        if itype == "function_call":
                            tool_index += 1
                            have_tools = True
                            yield _chunk_tool_start(tool_index, item.get("call_id"), item.get("name"))
                            yield _chunk_tool_args(tool_index, item.get("arguments") or "{}")
                        elif itype == "message" and not saw_text:
                            if (text := _extract_output_text(item)):
                                yield _chunk_text(text)
                    elif etype in ("response.completed", "response.incomplete"):
                        # response.incomplete is a TERMINAL event (e.g. the model
                        # spent the whole output budget on reasoning:
                        # incomplete_details.reason == "max_output_tokens").
                        # Ignoring it ended the turn with no finish and no usage —
                        # indistinguishable from a dead stream.
                        response_output = (ev.get("response") or {}).get("output") or []
                        for backfill in response_output:
                            if (backfill.get("type") == "reasoning"
                                    and backfill.get("id") in reasoning_by_id
                                    and backfill.get("encrypted_content")):
                                reasoning_by_id[backfill["id"]]["encrypted_content"] = (
                                    backfill["encrypted_content"])
                        provider_items = [item for _, _, item in sorted(
                            captured_meta,
                            key=lambda rec: (rec[0] if rec[0] is not None else rec[1], rec[1]))
                            if item.get("type") != "reasoning"
                            or item.get("encrypted_content")
                            or ((target or {}).get("retain_plaintext_reasoning")
                                and _has_reasoning_text(item))]
                        usage = (ev.get("response") or {}).get("usage") or {}
                        in_tok = int(usage.get("input_tokens") or 0)
                        out_tok = int(usage.get("output_tokens") or 0)
                        details = usage.get("input_tokens_details") or {}
                        cache_read = int(details.get("cached_tokens") or 0)
                        # The LLM Gateway nests cache_write_tokens under
                        # input_tokens_details (same place as cached_tokens);
                        # read there first, falling back to a top-level emitter.
                        cache_write = int(details.get("cache_write_tokens")
                                          or usage.get("cache_write_tokens") or 0)
                        _incomplete_reason = ((ev.get("response") or {})
                                              .get("incomplete_details") or {}).get("reason")
                        finish = ("tool_calls" if have_tools
                                  else "length" if (etype == "response.incomplete"
                                                    and _incomplete_reason == "max_output_tokens")
                                  else "stop")
                        yield {"choices": [{"index": 0, "delta": {},
                                            "finish_reason": finish}],
                               "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                                         "total_tokens": in_tok + out_tok,
                                         "cacheRead": cache_read, "cacheWrite": cache_write,
                                         "raw": dict(usage)},
                               "providerItems": provider_items}
                        break
                    elif etype in ("response.failed", "error"):
                        err = ((ev.get("response") or {}).get("error")
                               or ev.get("error") or ev)
                        raise UpstreamError(502, json.dumps(err))
        finally:
            await client.aclose()
        return
