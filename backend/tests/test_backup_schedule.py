"""Scheduled local backups (FR-X3): opt-in, interval-gated, retention-pruned,
wired into the maintenance loop and exposed over /api/backup/schedule."""

import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import config
from storage import backup
from storage import settings as settings_store


def _seed(home):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(home / "app.db"))
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO agents VALUES ('a1', 'Triage')")
    conn.commit(); conn.close()
    (home / "settings.json").write_text(json.dumps({"activeModel": "qwen", "api_key": "sk-keepme"}))


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_disabled_by_default_and_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    assert backup.get_schedule()["enabled"] is False
    assert backup.scheduled_backup_once() == {"ran": False, "reason": "disabled"}
    assert not (tmp_path / "backups").exists()


def test_set_schedule_clamps_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    s = backup.set_schedule({"enabled": True, "intervalHours": 0, "keep": 9999, "redactSecrets": None})
    assert s["enabled"] is True and s["intervalHours"] == 1 and s["keep"] == 365
    assert s["redactSecrets"] is False  # None leaves the default alone
    assert s["nextDueAt"]  # due immediately: never ran
    assert settings_store.get("backup")["schedule"]["keep"] == 365


def test_runs_when_due_and_keeps_secrets_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    backup.set_schedule({"enabled": True, "intervalHours": 24, "keep": 3})
    res = backup.scheduled_backup_once()
    assert res["ran"] is True and res["file"].startswith("scheduled-")
    path = tmp_path / "backups" / res["file"]
    assert path.is_file() and not list((tmp_path / "backups").glob("*.part"))
    with zipfile.ZipFile(path) as zf:
        settings = json.loads(zf.read("settings.json"))
    assert settings["api_key"] == "sk-keepme"  # local snapshot, not redacted
    assert backup.get_schedule()["lastScheduledAt"]
    assert backup.get_schedule()["lastScheduledError"] is None

    # Not due again right away.
    assert backup.scheduled_backup_once() == {"ran": False, "reason": "not due"}
    # ...but is once the interval has passed.
    cfg = settings_store.get("backup"); cfg["lastScheduledAt"] = _ago(25); settings_store.set("backup", cfg)
    assert backup.scheduled_backup_once()["ran"] is True


def test_redact_option_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    backup.set_schedule({"enabled": True, "redactSecrets": True})
    res = backup.scheduled_backup_once()
    with zipfile.ZipFile(tmp_path / "backups" / res["file"]) as zf:
        assert json.loads(zf.read("settings.json"))["api_key"] == backup.REDACTED


def test_retention_prunes_only_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    backups = tmp_path / "backups"; backups.mkdir()
    for i in range(5):
        (backups / f"scheduled-2026-01-0{i + 1}T000000.vwbak").write_bytes(b"x")
    (backups / "pre-import-2026-01-01T000000.bak.vwbak").write_bytes(b"x")
    backup.set_schedule({"enabled": True, "keep": 2})
    res = backup.scheduled_backup_once(force=True)
    assert res["ran"] is True
    names = sorted(p.name for p in backups.glob("*.vwbak"))
    scheduled = [n for n in names if n.startswith("scheduled-")]
    assert len(scheduled) == 2 and res["file"] in scheduled
    assert "pre-import-2026-01-01T000000.bak.vwbak" in names
    info = backup.last_backup_info()
    assert len(info["scheduled"]) == 2 and len(info["rollbacks"]) == 1
    assert info["schedule"]["keep"] == 2


def test_failure_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    backup.set_schedule({"enabled": True})
    monkeypatch.setattr(backup, "export_backup", lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")))
    res = backup.scheduled_backup_once()
    assert res["ran"] is False and "disk full" in res["error"]
    assert "disk full" in backup.get_schedule()["lastScheduledError"]


def test_maintenance_loop_runs_scheduled_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    from runtime import maintenance
    from storage import debug_db
    debug_db._schema_ready = False
    backup.set_schedule({"enabled": True})
    out = maintenance.prune_once()
    assert out["backup"] is True
    assert list((tmp_path / "backups").glob("scheduled-*.vwbak"))


def test_snapshot_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "scheduled-x.vwbak").write_bytes(b"x")
    (tmp_path / "secret.vwbak").write_bytes(b"x")
    assert backup.snapshot_path("scheduled-x.vwbak") is not None
    assert backup.snapshot_path("../secret.vwbak") is None
    assert backup.snapshot_path("scheduled-x.zip") is None
    assert backup.snapshot_path("missing.vwbak") is None


def test_schedule_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _seed(tmp_path)
    from fastapi.testclient import TestClient
    from app import app
    with TestClient(app) as c:
        r = c.put("/api/backup/schedule", json={"enabled": True, "intervalHours": 6, "keep": 2})
        assert r.status_code == 200 and r.json()["intervalHours"] == 6
        assert c.get("/api/backup/schedule").json()["enabled"] is True
        r = c.post("/api/backup/schedule/run")
        assert r.status_code == 200 and r.json()["ran"] is True
        name = r.json()["file"]
        assert "manifest" not in r.json()
        info = c.get("/api/backup/info").json()
        assert info["scheduled"][0]["name"] == name
        r = c.get(f"/api/backup/snapshots/{name}")
        assert r.status_code == 200 and r.content[:2] == b"PK"
        r = c.get("/api/backup/snapshots/..%2Fsettings.json")  # falls through, never the file
        assert b"sk-keepme" not in r.content
        assert c.delete(f"/api/backup/snapshots/{name}").status_code == 200
        assert c.get("/api/backup/info").json()["scheduled"] == []
