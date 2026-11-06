"""The Debug trace must record the FULL conversation a run received — system,
every prior turn (history), and the current user message — not just the first
user line. A multi-turn run (chat channel/chat with history) otherwise showed the
OLDEST message as "USER" and hid the real question + the prior context (making a
turn that answered from history look like it invented results with no tools).
See agent/loop.py:run / _record."""

import asyncio

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


def _answering_stream(text):
    async def stream(payload, provider=None):
        yield _chunk(delta={"content": text})
        yield _chunk(finish="stop", usage={"completion_tokens": 4})
    return stream


def _drive(stream, messages, monkeypatch, **kw):
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run(messages, model="local", tools={}, **kw):
            pass
    asyncio.run(collect())


def test_trace_records_full_conversation_and_current_question(home, monkeypatch):
    messages = [
        {"role": "user", "content": "check empty tote retrieval in the BRD"},
        {"role": "assistant", "content": "I grepped: no results in any BRD file."},
        {"role": "user", "content": "klo di BRD ada mention tentang itu ga?"},
    ]
    _drive(_answering_stream("Nggak ke-mention, mas."), messages, monkeypatch,
           session_id="channel:a1:emp-1", system="You are an SDET.")

    run = debug_db.get_session("channel:a1:emp-1")["runs"][0]
    types = [e["type"] for e in run["entries"]]
    # full incoming conversation, in order, then this run's own answer
    assert types == ["system", "user", "assistant", "user", "assistant"]

    users = [e["text"] for e in run["entries"] if e["type"] == "user"]
    assert users[0] == "check empty tote retrieval in the BRD"     # history is visible
    assert users[-1] == "klo di BRD ada mention tentang itu ga?"   # the REAL question shows

    # the run preview points at the CURRENT question, not the oldest history line
    assert run["firstUserText"] == "klo di BRD ada mention tentang itu ga?"


def _failing_stream(exc):
    async def stream(payload, provider=None):
        raise exc
        yield  # pragma: no cover — makes this an async generator
    return stream


def test_trace_records_upstream_error_as_visible_entry(home, monkeypatch):
    # A turn that dies on an upstream error must leave a visible error step in the
    # trace (an ``llm_fallback`` entry with the message), not just run-level status
    # — otherwise the Debug trace shows nothing about why the turn stopped.
    from gateway.upstream import UpstreamError
    _drive(_failing_stream(UpstreamError(502, "anthropic unreachable: All connection attempts failed")),
           [{"role": "user", "content": "hi"}], monkeypatch, session_id="chat:err")

    run = debug_db.get_session("chat:err")["runs"][0]
    assert run["statusCode"] == 500
    fallback = [e for e in run["entries"] if e["type"] == "llm_fallback"]
    assert len(fallback) == 1
    assert "anthropic unreachable" in fallback[0]["text"]
    assert fallback[0]["isError"] is True


def test_trace_single_turn_unchanged(home, monkeypatch):
    # A plain single-turn run still records system + the one user message + answer.
    _drive(_answering_stream("hi!"), [{"role": "user", "content": "hello"}], monkeypatch,
           session_id="chat:x", system="be nice")
    run = debug_db.get_session("chat:x")["runs"][0]
    assert [e["type"] for e in run["entries"]] == ["system", "user", "assistant"]
    assert run["firstUserText"] == "hello"


def test_no_clock_injection_anywhere(home, monkeypatch):
    # The prompt carries no injected clock: not in the persona (would break the
    # cached prefix) and not folded into the user turn (confused small local
    # models — it read as user text). What the caller sends is what goes out.
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what time is it?"},
    ]
    _drive(_answering_stream("noon"), messages, monkeypatch,
           session_id="chat:tc", system="You are helpful.")

    run = debug_db.get_session("chat:tc")["runs"][0]
    entries = run["entries"]
    assert [e["type"] for e in entries] == ["system", "user", "assistant", "user", "assistant"]
    assert "Current date & time" not in " ".join(e["text"] for e in entries)
    last_user = [e["text"] for e in entries if e["type"] == "user"][-1]
    assert last_user == "what time is it?"      # untouched — nothing appended


def test_repeat_penalty_sent_local_only(home, monkeypatch):
    # Small fine-tunes can lock into a phrase cycle; the local upstream gets a
    # repetition penalty under both spellings (mlx_lm reads repetition_penalty,
    # llama.cpp reads repeat_penalty). Remote providers reject unknown params,
    # so neither key may leave with a provider target.
    seen: list[dict] = []

    def capturing_stream(text):
        async def stream(payload, provider=None):
            seen.append(payload)
            yield _chunk(delta={"content": text})
            yield _chunk(finish="stop", usage={"completion_tokens": 2})
        return stream

    _drive(capturing_stream("ok"), [{"role": "user", "content": "halo"}], monkeypatch,
           session_id="chat:rp1")
    assert seen[-1]["repetition_penalty"] == loop.REPEAT_PENALTY
    assert seen[-1]["repeat_penalty"] == loop.REPEAT_PENALTY

    _drive(capturing_stream("ok"), [{"role": "user", "content": "halo"}], monkeypatch,
           session_id="chat:rp2", provider={"kind": "openai", "baseUrl": "https://api.openai.com"})
    assert "repetition_penalty" not in seen[-1]
    assert "repeat_penalty" not in seen[-1]

    # Per-agent override wins over the env default; 1 (or 0) disables entirely.
    _drive(capturing_stream("ok"), [{"role": "user", "content": "halo"}], monkeypatch,
           session_id="chat:rp3", repeat_penalty=1.3)
    assert seen[-1]["repetition_penalty"] == 1.3
    _drive(capturing_stream("ok"), [{"role": "user", "content": "halo"}], monkeypatch,
           session_id="chat:rp4", repeat_penalty=1)
    assert "repetition_penalty" not in seen[-1]
