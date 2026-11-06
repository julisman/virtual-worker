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


def test_timeline_roundtrip(store):
    """The work timeline (narration + tool calls with results) must survive a
    save/reload — losing it degraded the chat UI's collapsible panel on refresh."""
    timeline = [
        {"kind": "text", "text": "Let me check the page."},
        {"kind": "tool", "step": {"name": "browser_open", "args": {"url": "https://x.test"},
                                  "done": True, "result": "Opened the page."}},
    ]
    store.upsert_session("s-tl", "tl", None, [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "done", "timeline": timeline,
         "steps": [{"name": "browser_open", "done": True}]},
    ])
    s = store.get_session("s-tl")
    assert s["messages"][1]["timeline"] == timeline
    assert s["messages"][0]["timeline"] is None   # absent stays absent


def test_sessions_route_model_keeps_timeline():
    """Pydantic silently drops unknown fields — MessageIn must declare timeline
    or the route strips it before the store ever sees it (the refresh bug)."""
    from routes.sessions import MessageIn

    m = MessageIn(role="assistant", content="ok",
                  timeline=[{"kind": "tool", "step": {"name": "browser_open", "done": True}}])
    dumped = m.model_dump()
    assert dumped["timeline"][0]["step"]["name"] == "browser_open"


def test_list_sessions_filtered_by_agent(store):
    store.upsert_session("a1", "t", None, [{"role": "user", "content": "x"}], agent_id="agent-a")
    store.upsert_session("a2", "t", None, [{"role": "user", "content": "y"}], agent_id="agent-a")
    store.upsert_session("b1", "t", None, [{"role": "user", "content": "z"}], agent_id="agent-b")
    assert {s["id"] for s in store.list_sessions(agent_id="agent-a")} == {"a1", "a2"}
    assert {s["id"] for s in store.list_sessions(agent_id="agent-b")} == {"b1"}
    assert len(store.list_sessions()) == 3  # no filter -> all


def test_delete(store):
    store.upsert_session("s4", "x", None, [{"role": "user", "content": "x"}])
    assert store.delete_session("s4") is True
    assert store.get_session("s4") is None
    assert store.delete_session("nope") is False
