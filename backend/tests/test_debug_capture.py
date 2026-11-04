"""End-to-end test for the API-Call Debugging capture + storage + read path."""

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point the trace DB at a temp file and give a fresh debug_db module."""
    monkeypatch.setenv("VW_HOME", str(tmp_path))
    import config
    importlib.reload(config)
    import storage.debug_db as ddb
    importlib.reload(ddb)
    return ddb


def test_streaming_chat_capture(db, monkeypatch):
    import middleware.api_debug as api_debug
    importlib.reload(api_debug)  # bind to the reloaded debug_db

    sse = (
        'data: {"model":"local-qwen","choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"model":"local-qwen","choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: {"model":"local-qwen","choices":[{"delta":{"tool_calls":[{"index":0,'
        '"id":"call_1","function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}\n\n'
        'data: {"model":"local-qwen","choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"Jakarta\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":120,'
        '"completion_tokens":18,"total_tokens":138,"prompt_tokens_details":{"cached_tokens":64}}}\n\n'
        'data: [DONE]\n\n'
    )
    req = (b'{"model":"local-qwen","stream":true,"messages":['
           b'{"role":"system","content":"You are helpful"},'
           b'{"role":"user","content":"weather in Jakarta?"}]}')

    api_debug._capture_and_persist(
        "sess-test", "POST", "/v1/chat/completions", 200, 432,
        "2026-06-26T10:00:00+00:00", req, sse.encode(),
    )

    sessions = db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["sessionId"] == "sess-test"
    assert sessions[0]["toolCalls"] == 1

    sess = db.get_session("sess-test")
    run = sess["runs"][0]
    assert run["model"] == "local-qwen"
    assert run["provider"] == "local"
    assert run["totalTokens"] == 138
    assert run["cacheRead"] == 64
    assert run["toolCalls"] == 1

    types = [e["type"] for e in run["entries"]]
    assert types == ["system", "user", "assistant", "tool_call"]

    assistant = next(e for e in run["entries"] if e["type"] == "assistant")
    assert assistant["text"] == "Hello world"
    assert assistant["usage"]["input"] == 120

    tool = next(e for e in run["entries"] if e["type"] == "tool_call")
    assert tool["toolName"] == "get_weather"
    assert tool["toolArgs"] == {"city": "Jakarta"}  # reassembled across SSE chunks


def test_error_capture(db):
    import middleware.api_debug as api_debug
    importlib.reload(api_debug)
    api_debug._capture_and_persist(
        "sess-err", "POST", "/v1/chat/completions", 500, 12,
        "2026-06-26T10:05:00+00:00", b"{}", b'{"error":{"message":"model crashed"}}',
    )
    sess = db.get_session("sess-err")
    fallbacks = [e for r in sess["runs"] for e in r["entries"] if e["type"] == "llm_fallback"]
    assert fallbacks and "model crashed" in fallbacks[0]["text"]
    assert db.summary()["totalErrors"] == 1
