"""Retention: the always-on app.db must stay bounded — debug traces and the
activity feed prune by age and by a hard row ceiling."""

from datetime import datetime, timedelta, timezone

import pytest

import config
from storage import activity_store, debug_db


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    # Each test gets a fresh db path; reset the cached schema flag so the new db
    # actually gets its tables created.
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _run(run_id: str, created_at: str) -> dict:
    return {"run_id": run_id, "session_id": "s1", "start_time": created_at,
            "created_at": created_at, "path": "/v1/chat", "model": "m"}


def test_debug_prune_by_age(db):
    debug_db.insert_run(_run("old", _iso(30)), [])
    debug_db.insert_run(_run("new", _iso(1)), [])
    deleted = debug_db.prune(max_age_days=14, max_rows=None)
    assert deleted == 1
    assert debug_db.get_run("old") is None
    assert debug_db.get_run("new") is not None


def test_debug_prune_by_row_cap(db):
    for i in range(5):
        debug_db.insert_run(_run(f"r{i}", _iso(i)), [])   # r0 newest ... r4 oldest
    deleted = debug_db.prune(max_age_days=None, max_rows=3)
    assert deleted == 2
    assert debug_db.get_run("r0") is not None             # newest kept
    assert debug_db.get_run("r4") is None                 # oldest dropped


def test_activity_prune_by_age_and_cap(db):
    # Age prune: an old row goes, a fresh one stays.
    with activity_store._conn() as conn:
        conn.execute("INSERT INTO activities (id, agent_id, task_id, type, summary, detail, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", ("a-old", "ag", None, "note", "old", None, _iso(200)))
    activity_store.log("ag", "note", "fresh")
    assert activity_store.prune(max_age_days=90, max_rows=None) == 1
    remaining = activity_store.list_for("ag")
    assert [r["summary"] for r in remaining] == ["fresh"]

    # Row cap: keep only the newest N globally.
    for i in range(5):
        activity_store.log("ag", "note", f"n{i}")
    activity_store.prune(max_age_days=None, max_rows=2)
    assert activity_store.count_for("ag") == 2
