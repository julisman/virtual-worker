"""Malformed tool-call JSON: when the model emits a tool call whose `arguments`
string isn't valid JSON (common when a big string arg like wiki_write's `content`
carries unescaped newlines/quotes), the loop must NOT silently drop to {} and
dispatch — that surfaces as a bogus "missing required argument" and the model
retries the same broken call forever. Instead it feeds back a clear
"bad arguments … invalid JSON" result so the model can re-emit. See agent/loop.py."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _stream_calling_raw(name, raw_args, n):
    """Model calls tool ``name`` ``n`` times with the *raw* arguments string
    ``raw_args`` (which may be invalid JSON), then answers. Records every payload
    sent to the model so the test can see what the model was fed back."""
    seen = {"payloads": [], "turn": 0}

    async def stream(payload, provider=None):
        i = seen["turn"]
        seen["turn"] += 1
        seen["payloads"].append({"messages": [dict(m) for m in payload.get("messages", [])]})
        if i < n:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": raw_args}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "ok, stopping"})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return stream, seen


def _wiki_registry():
    """A wiki_write-like tool that records how it was called, so the test can
    prove a malformed call never reaches the tool with empty args."""
    calls = []

    async def wiki_write(path: str = "", content: str = "", wiki: str = "") -> str:
        calls.append({"path": path, "content": content})
        if not path.strip():
            return "wiki_write needs a 'path'."
        if not content.strip():
            return "wiki_write needs 'content'."
        return f"wrote {path} ({len(content)} bytes)."

    reg = {"wiki_write": {"fn": wiki_write, "schema": {"type": "function", "function": {
        "name": "wiki_write", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "wiki": {"type": "string"}}}}}}}
    return reg, calls


def _drive(registry, stream, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "go"}],
                                 model="local", tools=registry):
            pass

    asyncio.run(collect())


def _last_tool_msg(payload):
    tools = [m for m in payload.get("messages", []) if m.get("role") == "tool"]
    return tools[-1]["content"] if tools else ""


# A large multi-line markdown `content` with an unescaped newline + quote — the
# classic shape a local model produces that isn't valid JSON.
MALFORMED = '{"path": "load-test-results.md", "content": "# Results\nThe "p95" was 12ms}'


def test_malformed_json_feeds_back_clear_error(home, monkeypatch):
    reg, calls = _wiki_registry()
    stream, seen = _stream_calling_raw("wiki_write", MALFORMED, 1)
    _drive(reg, stream, monkeypatch)
    # The model's 2nd turn (index 1) carries the tool result for the bad call.
    msg = _last_tool_msg(seen["payloads"][1])
    assert msg.startswith("bad arguments for wiki_write")
    assert "invalid JSON" in msg


def test_malformed_json_never_dispatches_to_tool(home, monkeypatch):
    # The tool must not run at all on a malformed call — the old behavior invoked
    # it with {} (empty path/content), producing the misleading "needs a 'path'".
    reg, calls = _wiki_registry()
    stream, seen = _stream_calling_raw("wiki_write", MALFORMED, 1)
    _drive(reg, stream, monkeypatch)
    assert calls == []


def test_valid_json_still_dispatches(home, monkeypatch):
    # Guardrail against over-correction: a well-formed call still reaches the tool.
    reg, calls = _wiki_registry()
    good = json.dumps({"path": "x.md", "content": "# Title\nbody"})
    stream, seen = _stream_calling_raw("wiki_write", good, 1)
    _drive(reg, stream, monkeypatch)
    assert len(calls) == 1
    assert calls[0]["path"] == "x.md"
    assert "wrote x.md" in _last_tool_msg(seen["payloads"][1])


def test_repeated_malformed_json_trips_consecutive_error_guard(home, monkeypatch):
    # Same malformed call 4× → the "bad arguments" shape is caught by the guard,
    # which appends the stop-retrying note by the 3rd identical error.
    reg, calls = _wiki_registry()
    stream, seen = _stream_calling_raw("wiki_write", MALFORMED, 4)
    _drive(reg, stream, monkeypatch)
    assert "same error 3 times" in _last_tool_msg(seen["payloads"][3])
