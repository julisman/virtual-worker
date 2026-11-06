"""Conversation compaction — the summarize helper + the /api/chat/compact route.
Hermetic: the upstream model call is monkeypatched (no model is loaded)."""

import asyncio

import pytest

from agent import summarize


# --------------------------------------------------------------------------- #
# summarize_messages
# --------------------------------------------------------------------------- #

def test_summarize_messages_concatenates_stream(monkeypatch):
    async def fake_stream(payload, provider=None):
        # sanity: the conversation text reaches the model
        assert "the moon" in payload["messages"][-1]["content"]
        for piece in ["Recap: ", "user asked ", "about the moon."]:
            yield {"choices": [{"delta": {"content": piece}}]}

    monkeypatch.setattr(summarize.egress, "stream", fake_stream)
    out = asyncio.run(summarize.summarize_messages("m", [
        {"role": "user", "content": "tell me about the moon"},
        {"role": "assistant", "content": "the moon orbits earth"},
    ]))
    assert out == "Recap: user asked about the moon."


def test_summarize_messages_empty_history_short_circuits(monkeypatch):
    called = {"n": 0}

    async def fake_stream(payload, provider=None):
        called["n"] += 1
        yield {"choices": [{"delta": {"content": "x"}}]}

    monkeypatch.setattr(summarize.egress, "stream", fake_stream)
    assert asyncio.run(summarize.summarize_messages("m", [])) == ""
    assert called["n"] == 0  # never hits the model


# --------------------------------------------------------------------------- #
# POST /api/chat/compact
# --------------------------------------------------------------------------- #

def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import chat

    async def fake_resolve(agent, model):
        return ("test-model", None)

    monkeypatch.setattr(chat, "resolve_target", fake_resolve)
    monkeypatch.setattr(chat.agent_store, "get_default_agent", lambda: {"id": "a"})
    monkeypatch.setattr(chat.agent_store, "get_agent", lambda i: {"id": i})

    app = FastAPI()
    app.include_router(chat.router, prefix="/api/chat")
    return TestClient(app), chat


def _long_msgs(n=10):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"message number {i} " + "blah " * 120} for i in range(n)]


def test_compact_returns_summary(monkeypatch):
    client, chat = _client(monkeypatch)

    async def fake_summarize(model, messages, provider=None):
        return "SHORT RECAP"

    monkeypatch.setattr(chat, "summarize_messages", fake_summarize)
    r = client.post("/api/chat/compact", json={"messages": _long_msgs(), "keepRecent": 4})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "SHORT RECAP"
    assert body["keptRecent"] == 4


def test_compact_trivial_history_is_noop(monkeypatch):
    client, chat = _client(monkeypatch)

    async def boom(*a, **k):
        raise AssertionError("summarizer should not run on trivial history")

    monkeypatch.setattr(chat, "summarize_messages", boom)
    r = client.post("/api/chat/compact",
                    json={"messages": [{"role": "user", "content": "hi"}], "keepRecent": 4})
    assert r.status_code == 200
    assert r.json()["summary"] is None


def test_chat_sse_strips_in_process_result_full(monkeypatch):
    client, chat = _client(monkeypatch)
    full = "x" * 5000

    async def fake_stream(*args, **kwargs):
        yield {"type": "tool_result", "id": "c1", "name": "load_skill",
               "result": full[:600], "resultFull": full, "truncatedChars": 4400}

    monkeypatch.setattr(chat, "stream_run", fake_stream)
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    wire = response.content.decode()
    assert response.status_code == 200
    assert "tool_result" in wire
    assert full[:600] in wire
    assert "resultFull" not in wire
    assert full not in wire
