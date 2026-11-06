"""Hermetic tests for live chat steering and run lifecycle cleanup."""

import asyncio
import copy

import pytest

import config
import storage.debug_db as debug_db
from agent import loop, run_registry


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _turn(text):
    return {"text": text, "tool_calls": [], "finish": "stop", "usage": {}}


def test_steer_delivered_before_first_model_call(home, monkeypatch):
    payloads = []
    run_id = None

    async def fake_stream(payload, provider=None, strip_think=False):
        payloads.append(copy.deepcopy(payload["messages"]))
        yield "end", _turn("answer")

    async def collect():
        nonlocal run_id
        async for event in loop.run(
            [{"role": "user", "content": "hello"}], model="local", tools={}
        ):
            if event["type"] == "run":
                run_id = event["runId"]
                assert run_registry.deliver(run_id, "STEERED-MSG") is True

    monkeypatch.setattr(loop, "_stream_turn", fake_stream)
    asyncio.run(collect())
    assert run_id is not None
    assert any(
        message.get("role") == "user" and message.get("content") == "STEERED-MSG"
        for message in payloads[0]
    )


def test_steer_after_completion_returns_false(home, monkeypatch):
    run_id = None

    async def fake_stream(payload, provider=None, strip_think=False):
        yield "end", _turn("final")

    async def collect():
        nonlocal run_id
        async for event in loop.run(
            [{"role": "user", "content": "hello"}], model="local", tools={}
        ):
            if event["type"] in {"run", "done"}:
                run_id = event["runId"]

    monkeypatch.setattr(loop, "_stream_turn", fake_stream)
    asyncio.run(collect())
    assert run_id is not None
    assert run_registry.deliver(run_id, "late") is False


def test_final_iteration_race_is_consumed(home, monkeypatch):
    payloads = []
    run_id = None
    calls = 0

    async def fake_stream(payload, provider=None, strip_think=False):
        nonlocal calls
        calls += 1
        payloads.append(copy.deepcopy(payload["messages"]))
        if calls == 1:
            assert run_id is not None
            assert run_registry.deliver(run_id, "RACE-MSG") is True
        yield "end", _turn("answer")

    async def collect():
        nonlocal run_id
        async for event in loop.run(
            [{"role": "user", "content": "hello"}], model="local", tools={}
        ):
            if event["type"] == "run":
                run_id = event["runId"]

    monkeypatch.setattr(loop, "_stream_turn", fake_stream)
    asyncio.run(collect())
    assert calls == 2
    assert any(
        message.get("role") == "user" and message.get("content") == "RACE-MSG"
        for message in payloads[1]
    )
    assert any(
        message.get("role") == "assistant" and message.get("content") == "answer"
        for message in payloads[1]
    )


def test_inbox_close_and_delivery_lifecycle():
    run_id = "test-inbox-close"
    inbox = run_registry.register(run_id)
    try:
        assert run_registry.deliver(run_id, "pending") is True
        assert inbox.close_if_empty() == (["pending"], False)
        assert inbox.close_if_empty() == ([], True)
        assert inbox.closed is True
        assert run_registry.deliver(run_id, "after-close") is False
    finally:
        run_registry.unregister(run_id)
    assert run_registry.deliver(run_id, "after-unregister") is False
