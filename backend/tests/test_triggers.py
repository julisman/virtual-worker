"""Triggers — event-driven agent runs (PRD-09, M1).

Covers trigger CRUD, file-change detection + dedup (no OS watcher, mtime-based),
and that a webhook fire resolves the agent and records the outcome — the agent
run itself is monkeypatched so no model/network is needed.
"""

import asyncio

import pytest

import config


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from storage import trigger_store
    return trigger_store


# ─── CRUD ────────────────────────────────────────────────────────────────────

def test_create_file_trigger(store, tmp_path):
    watched = tmp_path / "inbox"
    watched.mkdir()
    t = store.create("a1", "Inbox", "file", "Process {{event}}", path=str(watched))
    assert t["type"] == "file"
    assert t["path"] == str(watched)
    assert t["enabled"] is True
    assert t["fireCount"] == 0
    assert t["hookToken"] is None


def test_create_webhook_trigger_gets_token(store):
    t = store.create("a1", "Hook", "webhook", "Handle the payload")
    assert t["type"] == "webhook"
    assert t["hookToken"] and len(t["hookToken"]) >= 16
    # Resolvable by its token.
    assert store.get_by_token(t["hookToken"])["id"] == t["id"]


def test_create_rejects_bad_type_and_missing_fields(store):
    with pytest.raises(ValueError):
        store.create("a1", "x", "monitor", "p")           # unsupported type
    with pytest.raises(ValueError):
        store.create("a1", "x", "file", "p")              # missing path
    with pytest.raises(ValueError):
        store.create("a1", "x", "webhook", "")            # missing prompt


def test_update_toggle_and_scope(store, tmp_path):
    d = tmp_path / "w"; d.mkdir()
    t = store.create("a1", "T", "file", "p", path=str(d))
    store.create("a2", "other", "webhook", "p")
    assert len(store.list_for_agent("a1")) == 1            # scoped per agent
    off = store.update(t["id"], enabled=False)
    assert off["enabled"] is False
    assert len(store.list_enabled()) == 1                  # only a2's webhook left on
    assert store.delete(t["id"]) is True
    assert store.get(t["id"]) is None


# ─── file-change detection + dedup ───────────────────────────────────────────

def test_file_change_detected_once(store, tmp_path):
    from agent.triggers import detect_file_changes

    watched = tmp_path / "inbox"
    watched.mkdir()
    t = store.create("a1", "Inbox", "file", "Process it", path=str(watched))

    # Baseline empty → nothing yet.
    assert detect_file_changes(t) == []

    # A new file appears → detected exactly once (dedup on the second poll).
    (watched / "invoice.pdf").write_text("hello")
    changed = detect_file_changes(t)
    assert len(changed) == 1 and changed[0].endswith("invoice.pdf")
    assert detect_file_changes(t) == []                   # deduped, no refire

    # Modifying the file is a fresh change.
    (watched / "invoice.pdf").write_text("hello world!!")
    changed2 = detect_file_changes(t)
    assert len(changed2) == 1 and changed2[0].endswith("invoice.pdf")
    assert detect_file_changes(t) == []


def test_preexisting_files_do_not_fire(store, tmp_path):
    from agent.triggers import detect_file_changes

    watched = tmp_path / "inbox"
    watched.mkdir()
    (watched / "old.txt").write_text("already here")
    # Baseline is taken at creation, so pre-existing files are not events.
    t = store.create("a1", "Inbox", "file", "p", path=str(watched))
    assert detect_file_changes(t) == []


# ─── webhook fire path resolves the agent ────────────────────────────────────

def test_webhook_fire_resolves_agent_and_records(store, monkeypatch):
    from agent import triggers as trig_mod

    t = store.create("a1", "Hook", "webhook", "Handle {{event}}")

    # Pretend the agent exists; capture what the run receives.
    captured = {}
    monkeypatch.setattr(trig_mod.agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": "p"})

    async def fake_run(agent, prompt, session_id):
        captured["agentId"] = agent["id"]
        captured["prompt"] = prompt
        captured["session"] = session_id
        return "handled", None

    monkeypatch.setattr(trig_mod, "execute_run", fake_run)

    out = asyncio.run(trig_mod.triggers.fire_hook(t["hookToken"], {"order": 42}))
    assert out is not None
    assert captured["agentId"] == "a1"
    assert "42" in captured["prompt"]                     # payload threaded in
    assert captured["session"] == f"trigger:{t['id']}"

    after = store.get(t["id"])
    assert after["fireCount"] == 1
    assert after["lastStatus"] == "ok"
    assert after["lastResult"] == "handled"


def test_unknown_token_returns_none(store, monkeypatch):
    from agent import triggers as trig_mod

    async def fake_run(agent, prompt, session_id):  # should never be called
        raise AssertionError("must not fire for unknown token")

    monkeypatch.setattr(trig_mod, "execute_run", fake_run)
    assert asyncio.run(trig_mod.triggers.fire_hook("nope", {})) is None


def test_run_now_records_error_when_agent_missing(store, monkeypatch):
    from agent import triggers as trig_mod

    t = store.create("a1", "Hook", "webhook", "p")
    monkeypatch.setattr(trig_mod.agent_store, "get_agent", lambda aid: None)
    asyncio.run(trig_mod.triggers.run_now(t))
    after = store.get(t["id"])
    assert after["lastStatus"] == "error"
    assert "agent" in (after["lastError"] or "").lower()
