"""Consecutive-error guard: when a tool returns the same error 3× in a row, the
loop injects a stop-retrying note into the tool result so the model breaks the
retry cycle instead of looping to max_iterations. See agent/loop.py."""

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


def _stream_calling(name, n):
    """Model calls tool ``name`` ``n`` times (one per turn), then answers.
    Records every payload sent to the model so the test can inspect what the
    model actually saw."""
    seen = {"payloads": [], "turn": 0}

    async def stream(payload, provider=None):
        i = seen["turn"]
        seen["turn"] += 1
        # Snapshot the messages now: the loop reuses the same convo list object
        # across turns (see _fit_context), so keeping the live payload would let
        # later appends leak backwards into earlier captures.
        seen["payloads"].append({"messages": [dict(m) for m in payload.get("messages", [])]})
        if i < n:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps({"x": 1})}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "ok, stopping"})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return stream, seen


def _boom_registry():
    async def boom(**kw):
        raise RuntimeError("upstream 500")  # _dispatch → "boom error: upstream 500"
    return {"boom": {"fn": boom, "schema": {"type": "function", "function": {
        "name": "boom", "parameters": {"type": "object",
                                       "properties": {"x": {"type": "integer"}}}}}}}


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


WARN = "same error 3 times"


def test_builtin_error_trips_guard_after_three(home, monkeypatch):
    # boom errors as "boom error: …" — a built-in dispatch error the old prefix
    # check (only "[tool error]") missed entirely. Call it 4×, then answer.
    stream, seen = _stream_calling("boom", 4)
    _drive(_boom_registry(), stream, monkeypatch)
    # payload index 3 is the model's 4th turn — it carries the 3rd error result,
    # which the guard should have annotated.
    assert WARN in _last_tool_msg(seen["payloads"][3])


def test_guard_silent_before_three(home, monkeypatch):
    stream, seen = _stream_calling("boom", 4)
    _drive(_boom_registry(), stream, monkeypatch)
    # turns 1 and 2 carry the 1st/2nd error — no warning yet.
    assert WARN not in _last_tool_msg(seen["payloads"][1])
    assert WARN not in _last_tool_msg(seen["payloads"][2])


def test_normal_result_mentioning_error_not_flagged(home, monkeypatch):
    # A successful result that merely contains the word "error" must not be read
    # as a tool failure (anchored/name-keyed match guards against false positives).
    async def logs(**kw):
        return "0 error: everything nominal"  # not "logs error:" — a normal payload
    reg = {"logs": {"fn": logs, "schema": {"type": "function", "function": {
        "name": "logs", "parameters": {"type": "object",
                                       "properties": {"x": {"type": "integer"}}}}}}}
    stream, seen = _stream_calling("logs", 4)
    _drive(reg, stream, monkeypatch)
    assert WARN not in _last_tool_msg(seen["payloads"][3])
