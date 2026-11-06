"""Maintenance — retention pruning for traces, activity, deliveries, dedup ids, media."""

import os
import time
from datetime import datetime, timedelta, timezone

import config
from runtime import maintenance
from storage import activity_store, channel_store, chat_channel_store, debug_db


def _old(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _run(run_id, start):
    debug_db.insert_run({"run_id": run_id, "session_id": "s", "start_time": start, "end_time": start,
                         "duration_ms": 1, "method": "POST", "path": "/v1/chat/completions",
                         "endpoint": "chat", "provider": "local", "model": "m", "status_code": 200},
                        [{"type": "user", "text": "hi"}])


def test_prune_once_prunes_old_and_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    debug_db._schema_ready = False
    monkeypatch.setattr(config, "DEBUG_RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "DEBUG_MAX_ROWS", 100)
    monkeypatch.setattr(config, "ACTIVITY_RETENTION_DAYS", 90)
    monkeypatch.setattr(config, "ACTIVITY_MAX_ROWS", 1000)
    monkeypatch.setattr(config, "DELIVERY_RETENTION_DAYS", 90)
    monkeypatch.setattr(config, "CHAT_EVENT_RETENTION_DAYS", 7)
    monkeypatch.setattr(config, "CHANNEL_MEDIA_RETENTION_DAYS", 14)

    _run("old", _old(40)); _run("new", _old(1))
    with debug_db.get_connection() as conn:   # prune keys on created_at (insertion time)
        conn.execute("UPDATE debug_runs SET created_at=? WHERE run_id='old'", (_old(40),))
        conn.commit()
    with activity_store._conn() as conn:
        conn.execute("INSERT INTO activities (id, agent_id, type, summary, created_at) VALUES ('a1','ag','note','x',?)", (_old(100),))
        conn.execute("INSERT INTO activities (id, agent_id, type, summary, created_at) VALUES ('a2','ag','note','y',?)", (_old(1),))
    with channel_store._conn() as conn:
        conn.execute("INSERT INTO deliveries (id, ts, channel, source, status) VALUES ('d1',?,'chat','reply','sent')", (_old(100),))
        conn.execute("INSERT INTO deliveries (id, ts, channel, source, status) VALUES ('d2',?,'chat','reply','sent')", (_old(1),))
    chat_channel_store.seen_before("acct", "e-new")
    with chat_channel_store._conn() as conn:
        conn.execute("INSERT INTO chat_events (event_id, account_id, received_at) VALUES ('e-old','acct',?)", (_old(10),))
    media = tmp_path / "channels" / "acct" / "media"; media.mkdir(parents=True)
    old_f = media / "old.jpg"; old_f.write_bytes(b"x")
    new_f = media / "new.jpg"; new_f.write_bytes(b"y")
    os.utime(old_f, (time.time() - 20 * 86400, time.time() - 20 * 86400))

    out = maintenance.prune_once()
    assert out["debug"] >= 1 and out["activities"] == 1 and out["deliveries"] == 1
    assert out["chatEvents"] == 1 and out["media"] == 1 and out["checkpoint"] is True
    with debug_db.get_connection() as conn:
        ids = {r[0] for r in conn.execute("SELECT run_id FROM debug_runs")}
        assert ids == {"new"}
    assert channel_store.get_delivery("d1") is None and channel_store.get_delivery("d2") is not None
    assert not old_f.exists() and new_f.exists()


def test_step_failure_does_not_stop_the_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    debug_db._schema_ready = False
    monkeypatch.setattr(activity_store, "prune", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = maintenance.prune_once()
    assert str(out["activities"]).startswith("error:") and out["checkpoint"] is True
