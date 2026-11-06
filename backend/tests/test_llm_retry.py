"""Bounded retries for transient provider errors in the main agent turn."""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from gateway.upstream import UpstreamError


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


def _run(monkeypatch, stream, session_id="chat:retry", **config_values):
    monkeypatch.setattr(loop.egress, "stream", stream)
    for name, value in config_values.items():
        monkeypatch.setattr(config, name, value)
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(loop.asyncio, "sleep", no_sleep)

    async def collect():
        return [event async for event in loop.run(
            [{"role": "user", "content": "hello"}],
            model="local", tools={}, session_id=session_id)]

    return asyncio.run(collect()), sleeps


def _scripted_failures(failures, success_text="recovered"):
    calls = {"count": 0}

    async def stream(payload, provider=None):
        attempt = calls["count"]
        calls["count"] += 1
        if attempt < len(failures):
            yield _chunk(delta={"content": "partial"})
            raise failures[attempt]
        yield _chunk(delta={"content": success_text})
        yield _chunk(finish="stop", usage={"completion_tokens": 2})

    return stream, calls


def test_transient_failures_retry_then_complete(home, monkeypatch):
    stream, calls = _scripted_failures([
        UpstreamError(502, "first provider blip"),
        UpstreamError(502, "second provider blip"),
    ])
    events, sleeps = _run(monkeypatch, stream, LLM_RETRIES=2, LLM_BACKOFF_BASE=0.5)

    assert calls["count"] == 3
    assert sleeps == [0.5, 1.0]
    assert [event["type"] for event in events].count("status") == 2
    assert [event["type"] for event in events].count("text_reset") == 2
    assert events[-1]["type"] == "done"
    run = debug_db.get_session("chat:retry")["runs"][0]
    retries = [entry for entry in run["entries"] if entry["type"] == "llm_retry"]
    assert len(retries) == 2
    assert all(not entry["isError"] for entry in retries)
    assert not any(entry["type"] == "llm_fallback" for entry in run["entries"])


def test_non_transient_401_fails_without_retry(home, monkeypatch):
    stream, calls = _scripted_failures([UpstreamError(401, "unauthorized")])
    events, sleeps = _run(monkeypatch, stream, LLM_RETRIES=5)

    assert calls["count"] == 1
    assert sleeps == []
    assert next(event for event in events if event["type"] == "error")["message"] == "unauthorized"
    run = debug_db.get_session("chat:retry")["runs"][0]
    assert not any(entry["type"] == "llm_retry" for entry in run["entries"])
    assert sum(entry["type"] == "llm_fallback" for entry in run["entries"]) == 1


def test_exhausted_transient_failures_record_fallback(home, monkeypatch):
    stream, calls = _scripted_failures([
        UpstreamError(503, "provider overloaded") for _ in range(3)
    ])
    events, sleeps = _run(monkeypatch, stream, LLM_RETRIES=2, LLM_BACKOFF_BASE=0)

    assert calls["count"] == 3
    assert sleeps == [0, 0]
    assert any(event["type"] == "error" for event in events)
    run = debug_db.get_session("chat:retry")["runs"][0]
    assert sum(entry["type"] == "llm_retry" for entry in run["entries"]) == 2
    assert sum(entry["type"] == "llm_fallback" for entry in run["entries"]) == 1


def test_zero_retries_fails_on_single_attempt(home, monkeypatch):
    stream, calls = _scripted_failures([UpstreamError(502, "server_error")])
    events, sleeps = _run(monkeypatch, stream, LLM_RETRIES=0)

    assert calls["count"] == 1
    assert sleeps == []
    assert any(event["type"] == "error" for event in events)
    run = debug_db.get_session("chat:retry")["runs"][0]
    assert not any(entry["type"] == "llm_retry" for entry in run["entries"])
    assert sum(entry["type"] == "llm_fallback" for entry in run["entries"]) == 1
