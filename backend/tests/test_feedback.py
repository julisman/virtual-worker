"""Feedback capture: record, replace, list, counts, curated export, routes."""

import json

import pytest

import config
from storage import feedback_store as fb


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return fb


def test_record_list_and_counts(store):
    store.record({"sessionId": "s1", "messageId": "m1", "signal": "up", "userPrompt": "hi", "original": "hello"})
    store.record({"sessionId": "s1", "messageId": "m2", "signal": "down", "userPrompt": "2+2?", "original": "5"})
    store.record({"sessionId": "s1", "messageId": "m3", "signal": "edit", "userPrompt": "capital of France?",
                  "original": "Berlin", "correctedText": "Paris"})
    assert len(store.list_feedback()) == 3
    assert len(store.list_feedback(signal="up")) == 1
    assert store.counts() == {"up": 1, "down": 1, "edit": 1, "total": 3, "trainable": 2}


def test_edit_requires_correction_and_signal_validated(store):
    with pytest.raises(ValueError):
        store.record({"sessionId": "s", "messageId": "m", "signal": "edit", "userPrompt": "q", "original": "a"})
    with pytest.raises(ValueError):
        store.record({"sessionId": "s", "messageId": "m", "signal": "meh"})


def test_rerating_a_turn_overwrites(store):
    store.record({"sessionId": "s", "messageId": "m", "signal": "down", "userPrompt": "q", "original": "a"})
    store.record({"sessionId": "s", "messageId": "m", "signal": "up", "userPrompt": "q", "original": "a"})
    rows = store.list_feedback()
    assert len(rows) == 1 and rows[0]["signal"] == "up"


def test_export_curates_signals(store):
    store.record({"sessionId": "s1", "messageId": "m1", "signal": "up", "userPrompt": "Greet me", "original": "Hello there!"})
    store.record({"sessionId": "s1", "messageId": "m2", "signal": "down", "userPrompt": "What is 2+2?", "original": "It is 5."})
    store.record({"sessionId": "s1", "messageId": "m3", "signal": "edit", "userPrompt": "Capital of France?",
                  "original": "Berlin.", "correctedText": "Paris."})
    store.record({"sessionId": "s2", "messageId": "m9", "signal": "up", "userPrompt": "Greet me", "original": "Hello there!"})
    raw, summary = store.build_dataset()
    lines = [json.loads(l) for l in raw.decode().splitlines()]
    assert summary == {"examples": 2, "positives": 1, "corrections": 1, "skipped": 2, "bytes": len(raw)}
    assert lines[0]["messages"][1]["content"] == "Hello there!"
    assert lines[1]["messages"] == [{"role": "user", "content": "Capital of France?"},
                                    {"role": "assistant", "content": "Paris."}]


def test_export_empty_raises(store):
    store.record({"sessionId": "s", "messageId": "m", "signal": "down", "userPrompt": "q", "original": "a"})
    with pytest.raises(ValueError):
        store.build_dataset()


def test_routes(store):
    from fastapi.testclient import TestClient
    from app import app
    with TestClient(app) as c:
        r = c.post("/api/feedback", json={"sessionId": "s", "messageId": "1", "signal": "up",
                                          "userPrompt": "q", "original": "a", "agentId": "ag"})
        assert r.status_code == 200 and r.json()["signal"] == "up"
        fid = r.json()["id"]
        assert c.post("/api/feedback", json={"signal": "edit", "userPrompt": "q"}).status_code == 400
        assert c.get("/api/feedback/counts?agentId=ag").json()["trainable"] == 1
        assert len(c.get("/api/feedback?agentId=ag").json()["feedback"]) == 1
        r = c.get("/api/feedback/export")
        assert r.status_code == 200 and r.headers["X-Feedback-Examples"] == "1"
        assert json.loads(r.text.strip())["messages"][0]["content"] == "q"
        assert c.delete(f"/api/feedback/{fid}").status_code == 200
        assert c.delete(f"/api/feedback/{fid}").status_code == 404
        assert c.get("/api/feedback/export").status_code == 400
