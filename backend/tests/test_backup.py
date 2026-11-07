"""Backup / restore (PRD-11 M1): export a studio to .vwbak, import into a fresh
home, and verify fidelity, the pre-import rollback, models-exclusion, and secret
redaction."""

import io
import json
import sqlite3
import zipfile

import pytest

import config
from storage import backup


def _seed_home(home):
    """Seed a studio home with a app.db (tokens + an MCP server with a secret
    env), an settings.json, a skill, and a models/ file."""
    home.mkdir(parents=True, exist_ok=True)
    db = home / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, persona TEXT)"
    )
    conn.execute("INSERT INTO agents VALUES ('a1', 'Triage', 'helpful')")
    conn.execute(
        "CREATE TABLE api_tokens (id TEXT PRIMARY KEY, token_hash TEXT, prefix TEXT)"
    )
    conn.execute("INSERT INTO api_tokens VALUES ('t1', 'deadbeefhash', 'sk-')")
    conn.execute(
        "CREATE TABLE mcp_servers (id TEXT PRIMARY KEY, name TEXT, env TEXT)"
    )
    conn.execute(
        "INSERT INTO mcp_servers VALUES (?,?,?)",
        ("m1", "github", json.dumps({"GITHUB_TOKEN": "ghp_supersecret", "REGION": "us"})),
    )
    conn.commit()
    conn.close()

    (home / "settings.json").write_text(json.dumps({"activeModel": "qwen", "api_key": "sk-leak"}))

    skill_dir = home / "skills" / "refund"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: Refund\n---\nBody")

    models_dir = home / "models" / "ft1"
    models_dir.mkdir(parents=True)
    (models_dir / "weights.bin").write_bytes(b"BIGMODELDATA" * 100)


def test_export_excludes_models_and_redacts_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(config, "APP_HOME", home)
    _seed_home(home)

    data, manifest = backup.export_backup()  # defaults: no models, redact on
    assert manifest["contents"]["models"] == 0
    assert manifest["includeModels"] is False
    assert manifest["contents"]["app.db"] is True
    assert manifest["contents"]["skills"] >= 1

    zf = zipfile.ZipFile(io.BytesIO(data))
    members = zf.namelist()
    assert "manifest.json" in members
    assert "app.db" in members
    assert "skills/refund/SKILL.md" in members
    assert not any(m.startswith("models/") for m in members)  # models excluded

    # The raw archive bytes must not contain plaintext secrets.
    assert b"ghp_supersecret" not in data
    assert b"deadbeefhash" not in data
    assert b"sk-leak" not in data

    # secretsNeeded surfaces the redacted MCP env key + the settings key.
    keys = {(s.get("type"), s.get("key")) for s in manifest["secretsNeeded"]}
    assert ("mcp", "GITHUB_TOKEN") in keys
    assert ("settings", "api_key") in keys


def test_include_models_opt_in(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(config, "APP_HOME", home)
    _seed_home(home)

    data, manifest = backup.export_backup(include_models=True)
    assert manifest["contents"]["models"] >= 1
    assert any(m.startswith("models/") for m in zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_export_then_import_into_fresh_home(tmp_path, monkeypatch):
    src = tmp_path / "src"
    monkeypatch.setattr(config, "APP_HOME", src)
    _seed_home(src)
    data, _ = backup.export_backup()

    # Import into a FRESH, empty home.
    dst = tmp_path / "dst"
    monkeypatch.setattr(config, "APP_HOME", dst)
    result = backup.import_backup(data)

    assert result["ok"] is True
    assert result["restored"]["app.db"] is True
    assert result["restored"]["skills"] is True
    # Fresh home had nothing to back up -> no pre-import rollback.
    assert result["preImportBackup"] is None

    # app.db restored with the agent row intact.
    conn = sqlite3.connect(str(dst / "app.db"))
    rows = conn.execute("SELECT name FROM agents").fetchall()
    conn.close()
    assert rows == [("Triage",)]

    # token hash dropped (not portable); MCP secret redacted, non-secret kept.
    conn = sqlite3.connect(str(dst / "app.db"))
    assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
    env = json.loads(conn.execute("SELECT env FROM mcp_servers").fetchone()[0])
    conn.close()
    assert env["GITHUB_TOKEN"] == backup.REDACTED
    assert env["REGION"] == "us"

    # settings + skill restored; settings secret redacted.
    settings = json.loads((dst / "settings.json").read_text())
    assert settings["activeModel"] == "qwen"
    assert settings["api_key"] == backup.REDACTED
    assert (dst / "skills" / "refund" / "SKILL.md").read_text().endswith("Body")
    # models excluded by default -> not present on the target.
    assert not (dst / "models").exists()


def test_import_creates_pre_import_rollback(tmp_path, monkeypatch):
    src = tmp_path / "src"
    monkeypatch.setattr(config, "APP_HOME", src)
    _seed_home(src)
    data, _ = backup.export_backup()

    # Import OVER an existing (populated) home -> a rollback backup must be made.
    dst = tmp_path / "dst"
    monkeypatch.setattr(config, "APP_HOME", dst)
    _seed_home(dst)
    result = backup.import_backup(data)

    pre = result["preImportBackup"]
    assert pre is not None
    from pathlib import Path
    pre_path = Path(pre)
    assert pre_path.exists()
    assert pre_path.name.endswith(".bak.vwbak")
    assert pre_path.parent == (dst / "backups")
    # The rollback is a valid, importable .vwbak.
    assert backup.read_manifest(pre_path.read_bytes())["version"] == backup.ARCHIVE_VERSION


def test_import_rejects_non_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "home")
    with pytest.raises(ValueError):
        backup.import_backup(b"not a zip")

    # A zip without a manifest is rejected.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app.db", b"x")
    with pytest.raises(ValueError):
        backup.import_backup(buf.getvalue())
