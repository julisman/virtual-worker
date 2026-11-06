# API-Call Debugging — capture middleware.

"""Pure-ASGI middleware that captures inbound LLM API calls into ``app.db``
so they can be inspected in the API-Debug viewer.

Design constraints:
  * Pure ASGI (not BaseHTTPMiddleware) so it never wraps/buffers streaming
    responses for the *client* — every event is forwarded to the real ``send``
    unchanged. We only *tee* a bounded copy of the bytes for our own parsing.
  * Capture is fully best-effort: any error in capture/parsing/persistence is
    swallowed so a debugging feature can never break inference.
  * Persistence (sqlite + JSON parsing) runs in a worker thread after the
    response has finished, off the request hot path.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from gateway.pricing import estimate_cost as _cost
from storage import debug_db

logger = logging.getLogger(__name__)

# Paths we treat as "an LLM API call". Both the /api/inference mount and the
# OpenAI-compatible /v1 mount are covered. Suffix-matched against the path.
_CAPTURE_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/messages",
    "/generate/stream",
    ":generateContent",        # Gemini (path: /v1beta/models/{model}:generateContent)
    ":streamGenerateContent",  # Gemini streaming
)
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024  # cap teed body at 2 MB


def _capture_enabled() -> bool:
    return os.environ.get("VW_DEBUG_CAPTURE", "1") not in ("0", "false", "False")


def _should_capture(scope) -> bool:
    if scope["type"] != "http":
        return False
    if scope.get("method", "").upper() != "POST":
        return False
    path = scope.get("path", "") or ""
    return any(path.endswith(s) or path.rstrip("/").endswith(s) for s in _CAPTURE_SUFFIXES)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _header(headers, name: bytes) -> str | None:
    for k, v in headers:
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


class ApiDebugMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not _capture_enabled() or not _should_capture(scope):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "POST")
        headers = scope.get("headers", [])
        # Session id: explicit header wins, else cookie-ish header, else fallback.
        session_id = (
            _header(headers, b"x-vw-session")
            or _header(headers, b"x-thread-id")
            or "adhoc"
        )

        req_chunks: list[bytes] = []
        req_size = 0

        async def receive_wrapper():
            nonlocal req_size
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body and req_size < _MAX_CAPTURE_BYTES:
                    req_chunks.append(body)
                    req_size += len(body)
            return message

        resp_chunks: list[bytes] = []
        resp_size = 0
        status_code = {"v": 200}

        async def send_wrapper(message):
            nonlocal resp_size
            if message["type"] == "http.response.start":
                status_code["v"] = message.get("status", 200)
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body and resp_size < _MAX_CAPTURE_BYTES:
                    resp_chunks.append(body)
                    resp_size += len(body)
            await send(message)  # forward unchanged — never block the client

        start = time.monotonic()
        start_iso = _now_iso()
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            req_bytes = b"".join(req_chunks)
            resp_bytes = b"".join(resp_chunks)
            try:
                # Persist off the hot path; do not await it blocking the response.
                loop = asyncio.get_event_loop()
                loop.run_in_executor(
                    None,
                    _capture_and_persist,
                    session_id, method, path, status_code["v"],
                    duration_ms, start_iso, req_bytes, resp_bytes,
                )
            except Exception:
                logger.debug("api_debug: failed to schedule persist", exc_info=True)


# --------------------------------------------------------------------------- #
# Parsing + persistence (runs in a worker thread)
# --------------------------------------------------------------------------- #

def _endpoint_label(path: str) -> str:
    for s in _CAPTURE_SUFFIXES:
        if path.endswith(s) or path.rstrip("/").endswith(s):
            return s.lstrip("/")
    return path


def _provider_of(path: str, model: str | None) -> str:
    p = path.lower()
    m = (model or "").lower()
    if "/v1beta/" in p or "generatecontent" in p or m.startswith("gemini"):
        return "google"
    if "/external/" in p or "/messages" in p or "/responses" in p:
        if "claude" in m or "anthropic" in m:
            return "anthropic"
        if "gpt" in m or "openai" in m or "o1" in m or "o3" in m:
            return "openai"
        return "external"
    if "claude" in m:
        return "anthropic"
    if m.startswith("gpt") or "openai" in m:
        return "openai"
    return "local"


def _as_text(content) -> str:
    """Coerce OpenAI/Anthropic message content (string OR list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("text", None) and block.get("text"):
                    parts.append(block["text"])
                elif block.get("type") == "input_text" and block.get("text"):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _truncate(s: str | None, n: int = 4000) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + f"\n…(+{len(s) - n} chars)"


def _parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into a list of JSON data objects (ignores [DONE])."""
    events = []
    for raw in text.split("\n\n"):
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except Exception:
                pass
    return events


def _reduce_openai_stream(events: list[dict]) -> dict:
    text_parts, thinking_parts = [], []
    tool_calls: dict[int, dict] = {}
    usage, finish_reason, model = None, None, None
    for ev in events:
        if ev.get("model"):
            model = ev["model"]
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices", []) or []:
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                thinking_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": tc.get("id"), "name": "", "args": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                if tc.get("id"):
                    slot["id"] = tc["id"]
    calls = []
    for _, slot in sorted(tool_calls.items()):
        args = slot["args"]
        try:
            args = json.loads(args) if args else {}
        except Exception:
            args = {"_raw": args}
        calls.append({"id": slot["id"], "name": slot["name"], "args": args})
    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts) or None,
        "tool_calls": calls,
        "usage": usage,
        "finish_reason": finish_reason,
        "model": model,
    }


def _parse_openai_json(obj: dict) -> dict:
    choice = (obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else (args or {})
        except Exception:
            args = {"_raw": args}
        calls.append({"id": tc.get("id"), "name": fn.get("name"), "args": args})
    return {
        "text": _as_text(msg.get("content")),
        "thinking": msg.get("reasoning_content"),
        "tool_calls": calls,
        "usage": obj.get("usage"),
        "finish_reason": choice.get("finish_reason"),
        "model": obj.get("model"),
    }


def _parse_anthropic_json(obj: dict) -> dict:
    text_parts, calls = [], []
    for block in obj.get("content", []) or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id"), "name": block.get("name"), "args": block.get("input") or {}})
    u = obj.get("usage") or {}
    usage = {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": (u.get("input_tokens", 0) + u.get("output_tokens", 0)),
        "prompt_tokens_details": {"cached_tokens": u.get("cache_read_input_tokens", 0)},
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
    } if u else None
    return {
        "text": "\n".join(text_parts),
        "thinking": None,
        "tool_calls": calls,
        "usage": usage,
        "finish_reason": obj.get("stop_reason"),
        "model": obj.get("model"),
    }


def _parse_gemini_json(obj: dict) -> dict:
    text_parts, calls = [], []
    cand = (obj.get("candidates") or [{}])[0]
    for part in (cand.get("content") or {}).get("parts", []) or []:
        if "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            calls.append({"id": None, "name": fc.get("name"), "args": fc.get("args") or {}})
    um = obj.get("usageMetadata") or {}
    usage = {
        "prompt_tokens": um.get("promptTokenCount", 0),
        "completion_tokens": um.get("candidatesTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", 0),
    } if um else None
    return {
        "text": "".join(text_parts), "thinking": None, "tool_calls": calls,
        "usage": usage, "finish_reason": cand.get("finishReason"),
        "model": obj.get("modelVersion"),
    }


def _reduce_gemini_stream(events: list[dict]) -> dict:
    text_parts, calls = [], []
    usage, finish, model = None, None, None
    for ev in events:
        cand = (ev.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts", []) or []:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                calls.append({"id": None, "name": fc.get("name"), "args": fc.get("args") or {}})
        if cand.get("finishReason"):
            finish = cand["finishReason"]
        if ev.get("usageMetadata"):
            um = ev["usageMetadata"]
            usage = {"prompt_tokens": um.get("promptTokenCount", 0),
                     "completion_tokens": um.get("candidatesTokenCount", 0),
                     "total_tokens": um.get("totalTokenCount", 0)}
        if ev.get("modelVersion"):
            model = ev["modelVersion"]
    return {"text": "".join(text_parts), "thinking": None, "tool_calls": calls,
            "usage": usage, "finish_reason": finish, "model": model}


def _parse_responses_json(obj: dict) -> dict:
    """OpenAI Responses non-stream object (status/output/usage)."""
    text_parts, thinking_parts, calls = [], [], []
    for item in obj.get("output", []) or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))
        elif t == "function_call":
            args = item.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) and args else (args or {})
            except Exception:
                args = {"_raw": args}
            calls.append({"id": item.get("call_id"), "name": item.get("name"), "args": args})
        elif t == "reasoning":
            for s in item.get("summary", []) or []:
                if s.get("text"):
                    thinking_parts.append(s["text"])
    u = obj.get("usage") or {}
    usage = {"prompt_tokens": u.get("input_tokens", 0),
             "completion_tokens": u.get("output_tokens", 0),
             "total_tokens": u.get("total_tokens", 0)} if u else None
    return {"text": "".join(text_parts), "thinking": "".join(thinking_parts) or None,
            "tool_calls": calls, "usage": usage,
            "finish_reason": obj.get("status"), "model": obj.get("model")}


def _reduce_responses_stream(events: list[dict]) -> dict:
    """Responses streaming: the final response.completed carries the full object."""
    completed = None
    text_parts = []
    for ev in events:
        if ev.get("type") == "response.completed" and ev.get("response"):
            completed = ev["response"]
        elif ev.get("type") == "response.output_text.delta" and ev.get("delta"):
            text_parts.append(ev["delta"])
    if completed:
        return _parse_responses_json(completed)
    return {"text": "".join(text_parts), "thinking": None, "tool_calls": [],
            "usage": None, "finish_reason": None, "model": None}


def _normalize_usage(usage: dict | None) -> dict:
    usage = usage or {}
    inp = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (inp + out)
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    return {
        "input": inp, "output": out, "totalTokens": total,
        "cacheRead": cache_read, "cacheWrite": cache_write,
    }


def _capture_and_persist(session_id, method, path, status, duration_ms,
                         start_iso, req_bytes, resp_bytes):
    """Worker-thread entry point. Fully guarded."""
    try:
        run_id = str(uuid.uuid4())
        end_iso = _now_iso()
        is_error = status >= 400

        # --- request ---
        req = {}
        try:
            req = json.loads(req_bytes.decode("utf-8")) if req_bytes else {}
        except Exception:
            req = {}
        model = req.get("model")
        stream = bool(req.get("stream")) or path.endswith(":streamGenerateContent")
        messages = req.get("messages") or []
        system_text = ""
        if isinstance(req.get("system"), str):
            system_text = req["system"]
        last_user = ""
        for m in messages:
            role = m.get("role")
            if role == "system" and not system_text:
                system_text = _as_text(m.get("content"))
            elif role == "user":
                last_user = _as_text(m.get("content"))

        # Gemini request shape: model in the path, contents/systemInstruction.
        if ":generateContent" in path or ":streamGenerateContent" in path:
            seg = path.rsplit("/", 1)[-1]
            model = model or seg.rpartition(":")[0] or None
            sys = req.get("systemInstruction") or req.get("system_instruction") or {}
            if not system_text and sys:
                system_text = "".join(p.get("text", "") for p in sys.get("parts", []) or [])
            for c in req.get("contents") or []:
                if c.get("role") in (None, "user"):
                    last_user = "".join(p.get("text", "") for p in c.get("parts", []) or []) or last_user
        # Responses request shape: instructions + input items.
        elif path.endswith("/responses"):
            if not system_text and isinstance(req.get("instructions"), str):
                system_text = req["instructions"]
            inp = req.get("input")
            if isinstance(inp, str):
                last_user = inp
            elif isinstance(inp, list):
                for item in inp:
                    if isinstance(item, dict) and item.get("role") == "user":
                        last_user = _as_text(item.get("content")) or last_user

        # --- response ---
        resp_text = ""
        try:
            resp_text = resp_bytes.decode("utf-8", errors="replace") if resp_bytes else ""
        except Exception:
            resp_text = ""

        parsed = {"text": "", "thinking": None, "tool_calls": [], "usage": None,
                  "finish_reason": None, "model": None}
        err_text = None
        if is_error:
            err_text = _truncate(resp_text, 4000)
        else:
            try:
                stripped = resp_text.lstrip()
                if "data:" in resp_text[:64] or stream:
                    events = _parse_sse(resp_text)
                    first = events[0] if events else {}
                    if any(e.get("type", "").startswith("response.") for e in events):
                        parsed = _reduce_responses_stream(events)
                    elif any("candidates" in (e or {}) for e in events):
                        parsed = _reduce_gemini_stream(events)
                    elif "content" in first and "choices" not in first:
                        parsed = _parse_anthropic_json(events[-1])  # best-effort
                    else:
                        parsed = _reduce_openai_stream(events)
                elif stripped.startswith("["):  # Gemini non-SSE streaming = JSON array
                    parsed = _reduce_gemini_stream(json.loads(stripped))
                elif stripped.startswith("{"):
                    obj = json.loads(stripped)
                    if obj.get("object") == "response" or "output" in obj and "choices" not in obj:
                        parsed = _parse_responses_json(obj)
                    elif "candidates" in obj:
                        parsed = _parse_gemini_json(obj)
                    elif "choices" in obj:
                        parsed = _parse_openai_json(obj)
                    elif "content" in obj and isinstance(obj.get("content"), list):
                        parsed = _parse_anthropic_json(obj)
                    else:
                        parsed["text"] = _truncate(resp_text, 4000) or ""
            except Exception:
                logger.debug("api_debug: response parse failed", exc_info=True)
                parsed["text"] = _truncate(resp_text, 2000) or ""

        model = parsed.get("model") or model
        provider = _provider_of(path, model)
        # External agents (Claude Code/Codex/Gemini) send no session header, so
        # bucket their calls per agent+model — one rolling call trail per client.
        if session_id == "adhoc" and provider != "local":
            short = (model or "").split("/")[-1]
            session_id = f"{provider}:{short}" if short else provider
        usage = _normalize_usage(parsed.get("usage"))
        cost = _cost(model, usage["input"], usage["output"])
        tool_calls = parsed.get("tool_calls") or []

        # --- assemble entries (readable trace, mirrors the asetin debug view) ---
        entries: list[dict] = []
        if system_text:
            entries.append({"type": "system", "text": _truncate(system_text, 2000),
                            "timestamp": start_iso})
        if last_user:
            entries.append({"type": "user", "text": _truncate(last_user, 4000),
                            "timestamp": start_iso})
        if is_error:
            entries.append({
                "type": "llm_fallback", "text": err_text, "is_error": True,
                "model": model, "provider": provider, "timestamp": end_iso,
                "tool_args": {"reason": f"HTTP {status}", "preview": _truncate(last_user, 200)},
            })
        else:
            entries.append({
                "type": "assistant",
                "text": _truncate(parsed.get("text"), 8000),
                "thinking": _truncate(parsed.get("thinking"), 8000),
                "model": model, "provider": provider, "timestamp": end_iso,
                "usage": {**usage, "cost": cost},
                "stop_reason": parsed.get("finish_reason"),
            })
            for tc in tool_calls:
                entries.append({
                    "type": "tool_call", "tool_name": tc.get("name"),
                    "tool_call_id": tc.get("id"), "tool_args": tc.get("args"),
                    "timestamp": end_iso, "model": model, "provider": provider,
                })

        run = {
            "run_id": run_id,
            "session_id": session_id or "adhoc",
            "start_time": start_iso,
            "end_time": end_iso,
            "duration_ms": duration_ms,
            "method": method,
            "path": path,
            "endpoint": _endpoint_label(path),
            "provider": provider,
            "model": model,
            "status_code": status,
            "is_error": is_error,
            "error_text": err_text,
            "stream": stream,
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "cache_read": usage["cacheRead"],
            "cache_write": usage["cacheWrite"],
            "total_tokens": usage["totalTokens"],
            # Cost is computed authoritatively inside ``debug_db.insert_run`` from
            # the shared price table (the single source of truth) — never supplied
            # by callers. The per-entry ``cost`` above is kept for the trace view.
            "tool_calls": len(tool_calls),
            "first_user_text": _truncate(last_user, 280),
            "last_assistant_text": _truncate(parsed.get("text"), 280),
            "request_json": _truncate(json.dumps(req)[:20000], 20000) if req else None,
            "response_text": _truncate(parsed.get("text") or err_text, 8000),
            "created_at": _now_iso(),
        }
        debug_db.insert_run(run, entries)
    except Exception:
        logger.debug("api_debug: capture failed", exc_info=True)
