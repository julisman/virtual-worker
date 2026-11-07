"""Portable backup / restore of local Virtual Worker studio state (PRD-11, Milestone 1).

A ``.vwbak`` archive is a plain ``zip`` (stdlib :mod:`zipfile`) holding a
**consistent** snapshot of everything that makes a studio *yours*:

  - ``app.db``     — the SQLite DB (sessions, agents, memory, tasks, …),
                        copied via SQLite ``VACUUM INTO`` so a live WAL-mode DB
                        is never torn.
  - ``settings.json``— studio settings (active model, prefs).
  - ``skills/``       — the on-disk SKILL.md packs directory.
  - ``models/``       — fused fine-tunes, **opt-in only** (large).
  - ``manifest.json`` — self-describing: version, createdAt, host, app version,
                        contents counts, secret-handling mode, secretsNeeded.

Deliberately **excluded by default**: fine-tunes under ``models/`` (opt-in via
``include_models``) and the HuggingFace base-model cache (``~/.cache/huggingface``)
— never in a ``.vwbak`` (huge, re-downloadable).

**Secret hygiene (default ON):** the exported DB copy is scrubbed — ``api_tokens``
rows (token *hashes*) are dropped (tokens are re-minted on the target, never
portable) and secret-looking MCP env values are replaced with ``__REDACTED__``,
each recorded in the manifest's ``secretsNeeded`` list so the user knows what to
re-enter. ``settings.json`` gets the same key-based redaction pass.

**Destructive import is made safe:** :func:`import_backup` first writes a
full, *un-redacted* pre-import backup of the current state to
``<APP_HOME>/backups/pre-import-<ts>.bak.vwbak`` (a rollback point), then
extracts into a staging dir, verifies the DB opens, and atomically swaps it into
place — a failed import never corrupts the live studio.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

VESBAK_VERSION = "1"
APP_VERSION = "0.1.0"
MANIFEST_NAME = "manifest.json"
REDACTED = "__REDACTED__"

# Substrings that mark an env key as holding a secret value worth redacting.
_SECRET_HINTS = (
    "key", "token", "secret", "password", "passwd", "pat",
    "credential", "auth", "apikey", "access",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _looks_secret(key: str) -> bool:
    k = (key or "").lower()
    return any(h in k for h in _SECRET_HINTS)


# --------------------------------------------------------------------------- #
# Consistent DB snapshot + redaction
# --------------------------------------------------------------------------- #

def _vacuum_into(src: Path, dest: Path) -> bool:
    """Produce a quiescent copy of ``src`` at ``dest`` via ``VACUUM INTO``.

    Returns ``False`` if the source DB doesn't exist yet (fresh home). ``dest``
    must not already exist (SQLite refuses to overwrite).
    """
    if not src.exists():
        return False
    if dest.exists():
        dest.unlink()
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return True


def _redact_db(db: Path) -> list[dict]:
    """Scrub secrets from an exported DB copy. Returns the ``secretsNeeded`` list.

    - ``api_tokens`` rows are deleted entirely (hashes are not portable).
    - secret-looking ``mcp_servers.env`` values become ``__REDACTED__``.
    Both tables are created lazily by their stores, so absence is fine.
    """
    needed: list[dict] = []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "api_tokens" in tables:
            conn.execute("DELETE FROM api_tokens")
        if "mcp_servers" in tables:
            for r in conn.execute("SELECT id, name, env FROM mcp_servers").fetchall():
                raw = r["env"]
                if not raw:
                    continue
                try:
                    env = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(env, dict):
                    continue
                changed = False
                for k, v in list(env.items()):
                    if v and v != REDACTED and _looks_secret(k):
                        env[k] = REDACTED
                        changed = True
                        needed.append({"type": "mcp", "server": r["name"], "key": k})
                if changed:
                    conn.execute(
                        "UPDATE mcp_servers SET env=? WHERE id=?",
                        (json.dumps(env), r["id"]),
                    )
        conn.commit()
    finally:
        conn.close()
    return needed


def _redact_settings(data: dict) -> tuple[dict, list[dict]]:
    """Recursively replace secret-looking values in the settings dict."""
    needed: list[dict] = []

    def walk(obj, path):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(v, str) and v and _looks_secret(k):
                    out[k] = REDACTED
                    needed.append({"type": "settings", "key": ".".join([*path, k])})
                else:
                    out[k] = walk(v, [*path, str(k)])
            return out
        if isinstance(obj, list):
            return [walk(v, path) for v in obj]
        return obj

    return walk(data, []), needed


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def write_backup(fh, include_models: bool = False, redact_secrets: bool = True) -> dict:
    """Stream a ``.vwbak`` archive of the current studio state into ``fh`` (any
    writable binary file object) and return the manifest. The HF cache is never
    included; ``models/`` only when ``include_models`` is true. Writing to a
    file keeps multi-GB model folders out of memory."""
    home = config.APP_HOME
    secrets_needed: list[dict] = []
    contents = {"app.db": False, "settings.json": False, "skills": 0, "models": 0}
    buf = fh

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        db_copy = tdp / "app.db"
        has_db = _vacuum_into(config.db_path(), db_copy)
        if has_db and redact_secrets:
            secrets_needed.extend(_redact_db(db_copy))

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if has_db:
                zf.write(db_copy, "app.db")
                contents["app.db"] = True

            settings_path = home / "settings.json"
            if settings_path.exists():
                try:
                    settings = json.loads(settings_path.read_text())
                except (OSError, ValueError):
                    settings = {}
                if redact_secrets:
                    settings, needed = _redact_settings(settings)
                    secrets_needed.extend(needed)
                zf.writestr("settings.json", json.dumps(settings, indent=2))
                contents["settings.json"] = True

            skills_dir = home / "skills"
            if skills_dir.exists():
                for f in sorted(skills_dir.rglob("*")):
                    if f.is_file():
                        arc = Path("skills") / f.relative_to(skills_dir)
                        zf.write(f, str(arc))
                        contents["skills"] += 1

            if include_models:
                models_dir = home / "models"
                if models_dir.exists():
                    for f in sorted(models_dir.rglob("*")):
                        if f.is_file():
                            arc = Path("models") / f.relative_to(models_dir)
                            zf.write(f, str(arc))
                            contents["models"] += 1

            manifest = {
                "version": VESBAK_VERSION,
                "app": "virtual-worker",
                "appVersion": APP_VERSION,
                "createdAt": _now_iso(),
                "host": socket.gethostname(),
                "includeModels": include_models,
                "redactSecrets": redact_secrets,
                "secretsNeeded": secrets_needed,
                "contents": contents,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))

    return manifest


def export_backup(include_models: bool = False, redact_secrets: bool = True) -> tuple[bytes, dict]:
    """In-memory variant for the download route: ``(archive_bytes, manifest)``."""
    buf = io.BytesIO()
    manifest = write_backup(buf, include_models=include_models, redact_secrets=redact_secrets)
    return buf.getvalue(), manifest


def export_backup_to(path: Path, include_models: bool = False, redact_secrets: bool = True) -> dict:
    """Write the archive straight to ``path`` (via a ``.part`` file) and return the manifest."""
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with open(tmp, "wb") as fh:
            manifest = write_backup(fh, include_models=include_models, redact_secrets=redact_secrets)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return manifest


# --------------------------------------------------------------------------- #
# Import / restore (destructive — guarded by a pre-import backup + staging)
# --------------------------------------------------------------------------- #

def _pre_import_backup() -> Path | None:
    """Snapshot the current state (un-redacted, faithful rollback) before an
    import overwrites it. Returns the ``.bak.vwbak`` path, or ``None`` if there
    is nothing to back up (fresh home)."""
    home = config.APP_HOME
    has_state = any((home / n).exists() for n in ("app.db", "settings.json", "skills"))
    if not has_state:
        return None
    include_models = (home / "models").exists()
    backups = config.ensure_dir(home / "backups")
    path = backups / f"pre-import-{_ts()}.bak.vwbak"
    export_backup_to(path, include_models=include_models, redact_secrets=False)
    return path


def read_manifest(data: bytes) -> dict:
    """Parse and validate a ``.vwbak`` manifest without writing anything."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid .vwbak archive: {exc}") from exc
    if MANIFEST_NAME not in zf.namelist():
        raise ValueError("archive is missing manifest.json — not a .vwbak file")
    try:
        manifest = json.loads(zf.read(MANIFEST_NAME))
    except ValueError as exc:
        raise ValueError(f"corrupt manifest: {exc}") from exc
    ver = str(manifest.get("version"))
    if ver != VESBAK_VERSION:
        raise ValueError(
            f"unsupported archive version {ver!r} (this Virtual Worker reads version {VESBAK_VERSION})"
        )
    return manifest


def import_backup(data: bytes) -> dict:
    """Restore a ``.vwbak`` over the current studio (DESTRUCTIVE).

    Order of operations (abort-safe): validate manifest → write a pre-import
    rollback backup → extract to a staging dir (path-traversal guarded) → verify
    the DB opens → atomically swap ``app.db`` / ``settings.json`` / ``skills/``
    (and ``models/`` if present) into place.
    """
    manifest = read_manifest(data)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in zf.namelist() if n != MANIFEST_NAME and not n.endswith("/")]

    home = config.ensure_dir(config.APP_HOME)
    pre_backup = _pre_import_backup()

    staging = home / f".vwbak-import-{_ts()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    staging_resolved = staging.resolve()

    restored = {"app.db": False, "settings.json": False, "skills": False, "models": False}
    try:
        # Extract every member into staging, rejecting path traversal.
        for n in names:
            target = (staging / n).resolve()
            if not str(target).startswith(str(staging_resolved) + os.sep):
                raise ValueError(f"unsafe path in archive: {n}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(n))

        # app.db — verify it opens cleanly, then atomic swap; drop stale WAL/SHM.
        sdb = staging / "app.db"
        if sdb.exists():
            conn = sqlite3.connect(str(sdb))
            try:
                conn.execute("PRAGMA integrity_check")
            finally:
                conn.close()
            dest_db = home / "app.db"
            os.replace(str(sdb), str(dest_db))
            for ext in ("-wal", "-shm"):
                stale = Path(str(dest_db) + ext)
                if stale.exists():
                    stale.unlink()
            restored["app.db"] = True

        sj = staging / "settings.json"
        if sj.exists():
            os.replace(str(sj), str(home / "settings.json"))
            restored["settings.json"] = True

        for sub in ("skills", "models"):
            src = staging / sub
            if src.exists():
                dest = home / sub
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.move(str(src), str(dest))
                restored[sub] = True
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "ok": True,
        "manifest": manifest,
        "restored": restored,
        "preImportBackup": str(pre_backup) if pre_backup else None,
    }


# --------------------------------------------------------------------------- #
# Info
# --------------------------------------------------------------------------- #

def last_backup_info() -> dict:
    """Summary for the UI: last export time (from settings) + on-disk rollback
    backups under ``<APP_HOME>/backups/``."""
    from storage import settings as settings_store

    snapshots = list_snapshots()
    backup_cfg = settings_store.get("backup", {}) or {}
    return {
        "lastExportAt": backup_cfg.get("lastExportAt"),
        "rollbacks": [s for s in snapshots if s["kind"] == "rollback"],
        "scheduled": [s for s in snapshots if s["kind"] == "scheduled"],
        "schedule": get_schedule(),
        "homePath": str(config.APP_HOME),
    }


def mark_exported() -> None:
    """Record the most-recent successful export time in settings."""
    from storage import settings as settings_store

    cfg = settings_store.get("backup", {}) or {}
    cfg["lastExportAt"] = _now_iso()
    settings_store.set("backup", cfg)


# --------------------------------------------------------------------------- #
# Scheduled local backups (PRD FR-X3)
# --------------------------------------------------------------------------- #
#
# Opt-in. When enabled, the maintenance loop (hourly) calls ``scheduled_backup_once``
# which writes ``<APP_HOME>/backups/scheduled-<ts>.vwbak`` once the configured
# interval has elapsed since the last scheduled snapshot, then keeps only the
# newest ``keep`` of them. Pre-import rollback snapshots are never pruned here.
# Settings live under the ``backup`` key next to ``lastExportAt``:
#   schedule: {enabled, intervalHours, keep, includeModels, redactSecrets}
#   lastScheduledAt, lastScheduledError

SCHEDULE_DEFAULTS = {
    "enabled": False,
    "intervalHours": 24,
    "keep": 7,
    "includeModels": False,
    # A local snapshot that sits next to settings.json gains nothing from
    # redaction and a restore from it is only complete with the secrets in it.
    "redactSecrets": False,
}
SCHEDULED_PREFIX = "scheduled-"


def _backup_cfg() -> dict:
    from storage import settings as settings_store
    return dict(settings_store.get("backup", {}) or {})


def _save_backup_cfg(cfg: dict) -> None:
    from storage import settings as settings_store
    settings_store.set("backup", cfg)


def get_schedule() -> dict:
    cfg = _backup_cfg()
    sched = {**SCHEDULE_DEFAULTS, **(cfg.get("schedule") or {})}
    return {
        **sched,
        "lastScheduledAt": cfg.get("lastScheduledAt"),
        "lastScheduledError": cfg.get("lastScheduledError"),
        "nextDueAt": _next_due(sched, cfg.get("lastScheduledAt")) if sched["enabled"] else None,
    }


def set_schedule(values: dict) -> dict:
    cfg = _backup_cfg()
    sched = {**SCHEDULE_DEFAULTS, **(cfg.get("schedule") or {})}
    for key in SCHEDULE_DEFAULTS:
        if key in values and values[key] is not None:
            sched[key] = values[key]
    sched["enabled"] = bool(sched["enabled"])
    sched["intervalHours"] = max(1, min(24 * 30, int(sched["intervalHours"])))
    sched["keep"] = max(1, min(365, int(sched["keep"])))
    sched["includeModels"] = bool(sched["includeModels"])
    sched["redactSecrets"] = bool(sched["redactSecrets"])
    cfg["schedule"] = sched
    _save_backup_cfg(cfg)
    return get_schedule()


def _next_due(sched: dict, last_iso: str | None) -> str | None:
    if not last_iso:
        return _now_iso()
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return _now_iso()
    return (last + timedelta(hours=int(sched["intervalHours"]))).isoformat()


def _is_due(sched: dict, last_iso: str | None, now: datetime) -> bool:
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    return now - last >= timedelta(hours=int(sched["intervalHours"]))


def scheduled_backup_once(*, force: bool = False) -> dict:
    """Write one scheduled snapshot if the schedule is enabled and due (or
    ``force``), then prune old scheduled snapshots. Returns a small status
    dict; never raises (the maintenance loop calls this)."""
    cfg = _backup_cfg()
    sched = {**SCHEDULE_DEFAULTS, **(cfg.get("schedule") or {})}
    if not force and not sched["enabled"]:
        return {"ran": False, "reason": "disabled"}
    now = datetime.now(timezone.utc)
    if not force and not _is_due(sched, cfg.get("lastScheduledAt"), now):
        return {"ran": False, "reason": "not due"}

    backups = config.ensure_dir(config.APP_HOME / "backups")
    path = backups / f"{SCHEDULED_PREFIX}{_ts()}.vwbak"
    try:
        manifest = export_backup_to(path, include_models=bool(sched["includeModels"]),
                                    redact_secrets=bool(sched["redactSecrets"]))
    except Exception as exc:  # noqa: BLE001 — recorded for the UI, never raised
        logger.warning("scheduled backup failed: %s", exc)
        cfg = _backup_cfg()
        cfg["lastScheduledError"] = str(exc)[:500]
        _save_backup_cfg(cfg)
        return {"ran": False, "reason": "error", "error": str(exc)}

    cfg = _backup_cfg()
    cfg["lastScheduledAt"] = now.isoformat()
    cfg.pop("lastScheduledError", None)
    _save_backup_cfg(cfg)
    pruned = prune_scheduled(int(sched["keep"]))
    nbytes = path.stat().st_size
    logger.info("scheduled backup written: %s (%d bytes, pruned %d)", path.name, nbytes, pruned)
    return {"ran": True, "file": path.name, "bytes": nbytes, "pruned": pruned,
            "manifest": manifest}


def prune_scheduled(keep: int) -> int:
    """Keep only the newest ``keep`` scheduled snapshots. Returns the count removed."""
    backups = config.APP_HOME / "backups"
    if not backups.exists():
        return 0
    files = sorted(backups.glob(f"{SCHEDULED_PREFIX}*.vwbak"), reverse=True)
    removed = 0
    for f in files[max(0, keep):]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def list_snapshots() -> list[dict]:
    """Every ``.vwbak`` under ``<APP_HOME>/backups``, newest first, tagged by kind."""
    backups = config.APP_HOME / "backups"
    out: list[dict] = []
    if not backups.exists():
        return out
    for f in sorted(backups.glob("*.vwbak"), reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "name": f.name,
            "kind": "scheduled" if f.name.startswith(SCHEDULED_PREFIX) else "rollback",
            "size": st.st_size,
            "modifiedAt": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    return out


def snapshot_path(name: str) -> Path | None:
    """Resolve a snapshot file name safely (no path components, must exist)."""
    if not name or name != Path(name).name or not name.endswith(".vwbak"):
        return None
    p = config.APP_HOME / "backups" / name
    return p if p.is_file() else None
