"""Hermetic analytics attribution and latency tests."""

import importlib

import pytest


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as ddb
    ddb = importlib.reload(ddb)
    import storage.analytics_db as adb
    adb = importlib.reload(adb)
    return ddb, adb


def _run(ddb, run_id, session_id, start_time, *, tokens=10, duration_ms=100):
    ddb.insert_run({
        "run_id": run_id,
        "session_id": session_id,
        "start_time": start_time,
        "endpoint": "/v1/chat/completions",
        "provider": "local",
        "model": "local-qwen",
        "status_code": 200,
        "input_tokens": tokens // 2,
        "output_tokens": tokens - tokens // 2,
        "total_tokens": tokens,
        "duration_ms": duration_ms,
    }, [])


def _channel_session(sid, agent_id):
    from storage import chat_store
    chat_store.append_message(sid, "user", "hi", agent_id=agent_id)


def test_channel_sessions_attribute_to_agent(mods):
    ddb, adb = mods
    _channel_session("whatsapp:acct1:conv1", "AG1")
    _channel_session("whatsapp:acct1:conv2", "AG1")
    _run(ddb, "run-1", "whatsapp:acct1:conv1", "2026-08-20T10:00:00+00:00")
    _run(ddb, "run-2", "whatsapp:acct1:conv2", "2026-08-20T11:00:00+00:00")

    cards = {card["agentId"]: card
             for card in adb.scorecard("2026-08-20", "2026-08-20")}

    assert cards["AG1"]["calls"] == 2
    assert cards["AG1"]["sessions"] == 2
    assert None not in cards


def test_agent_filter_restricts_summary_and_trends(mods):
    ddb, adb = mods
    _channel_session("telegram:acct1:conv1", "AG1")
    _channel_session("telegram:acct1:conv9", "AG1")
    _channel_session("telegram:acct2:conv2", "AG2")
    _run(ddb, "run-1", "telegram:acct1:conv1", "2026-08-20T10:00:00+00:00", tokens=30)
    _run(ddb, "run-2", "telegram:acct1:conv9", "2026-08-20T11:00:00+00:00", tokens=20)
    _run(ddb, "run-3", "telegram:acct2:conv2", "2026-08-20T12:00:00+00:00", tokens=90)

    summary = adb.summary("2026-08-20", "2026-08-20", {"agent": "AG1"})
    assert summary["calls"] == 2
    assert summary["totalTokens"] == 50

    trends = adb.trends("2026-08-20", "2026-08-20", filters={"agent": "AG1"})
    assert len(trends) == 1
    assert trends[0]["calls"] == 2
    assert trends[0]["totalTokens"] == 50


def test_summary_includes_numeric_p95_latency(mods):
    ddb, adb = mods
    for index, duration in enumerate((100, 200, 300)):
        _run(ddb, f"run-{index}", "channel:AG1:conv1", f"2026-08-20T1{index}:00:00+00:00",
             duration_ms=duration)

    summary = adb.summary("2026-08-20", "2026-08-20")

    assert isinstance(summary["p95LatencyMs"], int)
    assert summary["p95LatencyMs"] == 300
