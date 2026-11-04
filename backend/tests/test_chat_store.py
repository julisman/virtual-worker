"""Chat session persistence."""

import pytest

import config
import storage.chat_store as cs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return cs


def test_upsert_get_list(store):
    store.upsert_session("s1", "First chat", "local-qwen", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "steps": [{"name": "web_search", "done": True}]},
    ])
    s = store.get_session("s1")
    assert s["title"] == "First chat"
    assert s["model"] == "local-qwen"
    assert len(s["messages"]) == 2
    assert s["messages"][1]["steps"][0]["name"] == "web_search"

    listed = store.list_sessions()
    assert len(listed) == 1
    assert listed[0]["messageCount"] == 2


def test_upsert_overwrites_messages(store):
    store.upsert_session("s2", "t", None, [{"role": "user", "content": "a"}])
    store.upsert_session("s2", "t", None, [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    s = store.get_session("s2")
    assert len(s["messages"]) == 2  # replaced, not appended


def test_images_roundtrip(store):
    store.upsert_session("s3", "img", None, [
        {"role": "user", "content": "look", "images": ["data:image/png;base64,AAAA"]}])
    s = store.get_session("s3")
    assert s["messages"][0]["images"] == ["data:image/png;base64,AAAA"]


def test_delete(store):
    store.upsert_session("s4", "x", None, [{"role": "user", "content": "x"}])
    assert store.delete_session("s4") is True
    assert store.get_session("s4") is None
    assert store.delete_session("nope") is False
