"""Parallel dispatch of side-effect-free tool calls: when a turn carries several
calls and a tool's registry entry is flagged ``parallel_safe``, the loop starts
them concurrently while still processing results strictly in order (dedup cache,
error guard, trace and streamed events are order-dependent). Unflagged tools run
sequentially exactly as before. See agent/loop.py."""

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


def _stream_three_calls(name):
    """One model turn with three calls to ``name`` (distinct args), then stop."""
    seen = {"turn": 0, "payloads": []}

    async def stream(payload, provider=None):
        i = seen["turn"]
        seen["turn"] += 1
        seen["payloads"].append({"messages": [dict(m) for m in payload.get("messages", [])]})
        if i == 0:
            yield _chunk(delta={"tool_calls": [
                {"index": k, "id": f"c{k}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps({"x": k})}}
                for k in range(3)]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 2})

    return stream, seen


def _slow_registry(*, parallel_safe):
    """A tool that sleeps briefly and tracks its own peak concurrency."""
    state = {"cur": 0, "max": 0}

    async def slow(x=0):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.1)
        state["cur"] -= 1
        return f"result-{x}"

    entry = {"fn": slow, "schema": {"type": "function", "function": {
        "name": "slow", "parameters": {"type": "object",
                                       "properties": {"x": {"type": "integer"}}}}}}
    if parallel_safe:
        entry["parallel_safe"] = True
    return {"slow": entry}, state


def _drive(registry, stream, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "go"}],
                                model="local", tools=registry):
            pass
    asyncio.run(collect())


def test_parallel_safe_calls_overlap_and_results_stay_ordered(home, monkeypatch):
    registry, state = _slow_registry(parallel_safe=True)
    stream, seen = _stream_three_calls("slow")
    _drive(registry, stream, monkeypatch)
    assert state["max"] >= 2          # the three dispatches actually overlapped
    # Results are fed back to the model in call order despite concurrent execution.
    tool_msgs = [m for m in seen["payloads"][1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1", "c2"]
    assert [m["content"] for m in tool_msgs] == ["result-0", "result-1", "result-2"]


def test_unflagged_calls_stay_sequential(home, monkeypatch):
    registry, state = _slow_registry(parallel_safe=False)
    stream, seen = _stream_three_calls("slow")
    _drive(registry, stream, monkeypatch)
    assert state["max"] == 1          # never more than one in flight
    tool_msgs = [m for m in seen["payloads"][1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1", "c2"]


def test_tool_call_event_carries_registry_kind(home, monkeypatch):
    # The loop attaches the registry entry's semantic `kind` to each tool_call
    # event so downstream consumers (chat channel status lines) don't name-sniff.
    registry, _ = _slow_registry(parallel_safe=False)
    registry["slow"]["kind"] = "repo"
    stream, _ = _stream_three_calls("slow")
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        events = []
        async for ev in loop.run([{"role": "user", "content": "go"}],
                                 model="local", tools=registry):
            if ev.get("type") == "tool_call":
                events.append(ev)
        return events

    events = asyncio.run(collect())
    assert events, "expected at least one tool_call event"
    assert all(ev["kind"] == "repo" for ev in events)


def test_tool_call_event_kind_is_none_when_unannotated(home, monkeypatch):
    registry, _ = _slow_registry(parallel_safe=False)   # no "kind" key
    stream, _ = _stream_three_calls("slow")
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        kinds = []
        async for ev in loop.run([{"role": "user", "content": "go"}],
                                 model="local", tools=registry):
            if ev.get("type") == "tool_call":
                kinds.append(ev["kind"])
        return kinds

    kinds = asyncio.run(collect())
    assert kinds and all(k is None for k in kinds)
