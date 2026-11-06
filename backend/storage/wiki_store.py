"""Agent Knowledge Wikis — the OKF storage layer (PRD-13, Milestone 1).

A wiki is a **compounding, LLM-maintained** knowledge base stored as plain
markdown files on disk, conformant to the Open Knowledge Format (OKF): each page
is a `.md` file with YAML frontmatter (a required ``type``) + a markdown body,
linked with ``[[wikilinks]]``, with reserved ``index.md`` (catalog) and
``log.md`` (history) files. Files on disk are the **source of truth** — inspect
them in any editor, diff them with git, take them to another tool.

Only a light **registry** lives in ``app.db`` (the ``wiki`` table: id, name,
description); the pages themselves are files under ``APP_HOME/wikis/<id>/``.
Search in this first cut is a pure-Python keyword scan over the files (no derived
index to keep in sync); FTS/embeddings are a later optimization (PRD-13 M4).

The agent-facing tools (read/write/search) live one layer up in ``agent/wiki``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import sqlite3
from storage import _sqlite
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import config
from config import db_path, ensure_dir
from storage import agent_store, linked_wiki

logger = logging.getLogger(__name__)

_lock = threading.Lock()

INDEX_FILE = "index.md"
LOG_FILE = "log.md"
_RESERVED = {INDEX_FILE, LOG_FILE}


class WikiError(Exception):
    """A wiki file-op problem worth reporting back to the model verbatim."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Trust tiers a linked repo can hold (linked-wiki revamp §3). Tier 3 (code-mode)
# is deliberately not offered yet.
_TRUST_TIERS = {"readonly", "repo-tooling"}


def _valid_trust_tier(tier: str) -> str:
    t = (tier or "readonly").strip() or "readonly"
    if t not in _TRUST_TIERS:
        raise WikiError(f"invalid trust tier {tier!r}; must be one of {sorted(_TRUST_TIERS)}")
    return t


def _resolve_root(root: Path) -> Path:
    return Path(root).expanduser().resolve(strict=False)


def _safe(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse anything that escapes it (``../``,
    absolute paths, symlinks pointing out) — the same confinement as fs_tools."""
    rel = (rel or "").strip()
    if rel in ("", "."):
        return root
    candidate = (root / rel) if not Path(rel).is_absolute() else Path(rel)
    resolved = candidate.expanduser().resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WikiError(f"path escapes the wiki: {rel!r}")
    return resolved


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            ingested_at TEXT
        )
    """)
    # PRD-21: preserve the existing managed bundle rows while allowing a row to
    # point at an external, read-only Markdown checkout.  SQLite migrations stay
    # guarded because existing Virtual Worker installs already have this table.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wiki)").fetchall()}
    for name, ddl in {
        "source_kind": "TEXT NOT NULL DEFAULT 'managed'",
        "root_path": "TEXT",
        "wiki_root": "TEXT",
        "recipe_json": "TEXT",
        "local_override_json": "TEXT",
        "trusted_operations_json": "TEXT",
        "observed_revision": "TEXT",
        "observed_at": "TEXT",
        "last_operation": "TEXT",
        "last_error": "TEXT",
        # Per-repo trust tier (linked-wiki revamp §3). Decided by the user via an
        # approved proposal — the agent can never self-escalate it.
        "trust_tier": "TEXT NOT NULL DEFAULT 'readonly'",
        # setup/sync surfaces (§4): first successful `setup`-kind run + last
        # successful `sync`-kind run, stamped by kind (not operation name).
        "setup_completed_at": "TEXT",
        "last_sync_at": "TEXT",
    }.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE wiki ADD COLUMN {name} {ddl}")
    # PRD-21 FR-15: the Wiki Repository job runner records each async operation
    # run (streamed logs land here on completion) — guarded like the columns above.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_repository_job (
            id TEXT PRIMARY KEY,
            wiki_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            argv_json TEXT,
            cwd TEXT,
            started_at TEXT,
            finished_at TEXT,
            exit_code INTEGER,
            output TEXT,
            error TEXT
        )
    """)
    # Tier-1 prerequisite: every read-only shell command a linked repo refuses is
    # logged per wiki, so demand for a writable tier is measurable (see the
    # linked-wiki revamp spec §3). Guarded like the tables above.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_shell_refusal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wiki_id TEXT NOT NULL,
            command TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Unified, persisted approval queue (linked-wiki revamp §5). Every
    # consent-requiring proposal — link a repo, change operations, change trust
    # tier — lives here and survives restarts. wiki_id is NULL for a `link`
    # proposal (no wiki exists yet). Guarded like the tables above.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_proposal (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            wiki_id TEXT,
            payload_json TEXT,
            status TEXT NOT NULL,
            user_note TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            executed_at TEXT
        )
    """)
    conn.mark_initialized()
    return conn


def _wikis_root() -> Path:
    """The on-disk home for all wiki bundles (resolved at call time so tests that
    monkeypatch ``config.APP_HOME`` land in the temp dir)."""
    root = Path(config.APP_HOME) / "wikis"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bundle(wiki_id: str) -> Path:
    return _resolve_root(_wikis_root() / wiki_id)


# --------------------------------------------------------------------------- #
# OKF frontmatter (lenient parse + serialize — no YAML dependency)
# --------------------------------------------------------------------------- #

_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(meta, body)``. Lenient: handles ``key: value``, inline
    ``[a, b]`` lists, and ``key:`` + ``- item`` block lists. No frontmatter ⇒
    ``({}, text)``."""
    m = _FM_RE.match(text or "")
    if not m:
        return {}, (text or "")
    meta: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None and isinstance(meta.get(key), list):
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val == "":
            meta[key] = []                       # a block list may follow
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = [_scalar(x) for x in val[1:-1].split(",") if x.strip()]
        else:
            meta[key] = _scalar(val)
    return meta, m.group(2)


def serialize_page(meta: dict, body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            if v:
                lines.append(f"{k}:")
                lines += [f"  - {x}" for x in v]
            else:
                lines.append(f"{k}: []")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def _meta_str(v) -> str:
    """A frontmatter value as a display string (lists → comma-joined)."""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v or "")


def ensure_okf(content: str) -> str:
    """Guarantee OKF conformance: parseable frontmatter with a non-empty
    ``type``, and a fresh ``timestamp``. Content with no frontmatter is wrapped."""
    meta, body = split_frontmatter(content or "")
    if not _meta_str(meta.get("type")).strip():
        meta["type"] = "Note"
    meta["timestamp"] = _now()
    return serialize_page(meta, body)


# --------------------------------------------------------------------------- #
# Registry CRUD
# --------------------------------------------------------------------------- #

def _page_files(wiki_id: str) -> list[Path]:
    base = _bundle(wiki_id)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def _wiki_row(r: sqlite3.Row) -> dict:
    linked = "source_kind" in r.keys() and r["source_kind"] == "linked_repository"
    if linked:
        try:
            _, root = linked_wiki.resolve_root(r["root_path"], r["wiki_root"] or ".")
            pages = linked_wiki.page_files(root)
        except (OSError, linked_wiki.LinkedWikiError):
            pages = []
    else:
        pages = [p for p in _page_files(r["id"]) if p.name not in _RESERVED]
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"] or "",
        "createdAt": r["created_at"],
        "ingestedAt": r["ingested_at"],
        "pageCount": len(pages),
        "sourceKind": "linked_repository" if linked else "managed",
        "readOnly": linked,
        "rootPath": (r["root_path"] if "root_path" in r.keys() else None),
        "wikiRoot": (r["wiki_root"] if "wiki_root" in r.keys() else None),
        "observedRevision": (r["observed_revision"] if "observed_revision" in r.keys() else None),
        "observedAt": (r["observed_at"] if "observed_at" in r.keys() else None),
        "lastOperation": (r["last_operation"] if "last_operation" in r.keys() else None),
        "lastError": (r["last_error"] if "last_error" in r.keys() else None),
        "trustTier": (r["trust_tier"] if "trust_tier" in r.keys() and r["trust_tier"] else "readonly"),
        "setupCompletedAt": (r["setup_completed_at"] if "setup_completed_at" in r.keys() else None),
        "lastSyncAt": (r["last_sync_at"] if "last_sync_at" in r.keys() else None),
    }


def create_wiki(name: str, description: str = "") -> dict:
    name = (name or "").strip() or "Untitled wiki"
    wiki_id = uuid.uuid4().hex[:12]
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO wiki (id, name, description, created_at, ingested_at) VALUES (?,?,?,?,?)",
            (wiki_id, name, (description or "").strip(), now, None),
        )
        r = conn.execute("SELECT * FROM wiki WHERE id=?", (wiki_id,)).fetchone()
    # Scaffold the OKF bundle: an index.md catalog + a log.md history.
    base = _bundle(wiki_id)
    base.mkdir(parents=True, exist_ok=True)
    (base / INDEX_FILE).write_text(
        f"# {name}\n\n{(description or '').strip()}\n\n_Pages appear here as the agent writes them._\n",
        encoding="utf-8")
    (base / LOG_FILE).write_text(f"# Log\n\n### {now}\n- created wiki **{name}**\n", encoding="utf-8")
    return _wiki_row(r)


def _json_object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _effective_recipe(source_recipe: dict, local_override: dict) -> dict:
    """Merge source-owned metadata with local-only operation overrides.

    The repository manifest stays versioned and readable, while commands entered
    through Virtual Worker are stored locally and therefore never write into the checkout.
    Local operations may replace a source operation of the same stable id.
    """
    source = source_recipe if isinstance(source_recipe, dict) else {}
    local = local_override if isinstance(local_override, dict) else {}
    source_ops = source.get("operations") if isinstance(source.get("operations"), dict) else {}
    local_ops = local.get("operations") if isinstance(local.get("operations"), dict) else {}
    return {**source, "operations": {**source_ops, **local_ops}}


def _row_recipe(row: sqlite3.Row) -> dict:
    source = _json_object(row["recipe_json"] if "recipe_json" in row.keys() else None)
    local = _json_object(row["local_override_json"] if "local_override_json" in row.keys() else None)
    return _effective_recipe(source, local)


def create_linked_wiki(name: str, repo_path: str, wiki_root: str = ".", description: str = "",
                       trusted_operations: list[str] | None = None,
                       operation_overrides: dict | None = None, trust_tier: str = "repo-tooling") -> dict:
    """Register a local checkout after a read-only probe.  No repository code runs."""
    tier = _valid_trust_tier(trust_tier)
    discovered = linked_wiki.probe(repo_path, wiki_root)
    repo, root = linked_wiki.resolve_root(discovered["repoPath"], discovered["wikiRoot"])
    recipe = discovered["manifest"]
    local_override = {"operations": operation_overrides or {}}
    operations = linked_wiki.normalize_operations(_effective_recipe(recipe, local_override), repo)
    trusted = sorted(set(trusted_operations or []) & set(operations))
    wiki_id, now = uuid.uuid4().hex[:12], _now()
    with _lock, _conn() as conn:
        conn.execute("""INSERT INTO wiki
            (id,name,description,created_at,ingested_at,source_kind,root_path,wiki_root,recipe_json,local_override_json,
             trusted_operations_json,observed_revision,observed_at,last_error,trust_tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wiki_id, (name or repo.name).strip() or repo.name, (description or "").strip(), now, None,
             "linked_repository", str(repo), _rel_to(repo, root), json.dumps(recipe), json.dumps(local_override), json.dumps(trusted),
             discovered["revision"], now, discovered.get("manifestError"), tier))
        row = conn.execute("SELECT * FROM wiki WHERE id=?", (wiki_id,)).fetchone()
    # Linked Wikis are useful only when an assistant can consume them.  Mirror
    # the product default by attaching a newly linked source to the seeded
    # Assistant; other agents remain opt-in through the existing agent settings.
    assistant = agent_store.get_default_agent()
    if assistant and wiki_id not in (assistant.get("wikis") or []):
        agent_store.update_agent(assistant["id"], {"wikis": [*(assistant.get("wikis") or []), wiki_id]})
    return _wiki_row(row)


def _rel_to(repo: Path, root: Path) -> str:
    try:
        return str(root.relative_to(repo)) or "."
    except ValueError:
        return "."


def _raw_wiki(wiki_id: str):
    with _conn() as conn:
        return conn.execute("SELECT * FROM wiki WHERE id=?", (wiki_id,)).fetchone()


def is_linked(wiki_id: str) -> bool:
    row = _raw_wiki(wiki_id)
    return bool(row and "source_kind" in row.keys() and row["source_kind"] == "linked_repository")


def _linked_roots(wiki_id: str) -> tuple[Path, Path, sqlite3.Row]:
    row = _raw_wiki(wiki_id)
    if not row or row["source_kind"] != "linked_repository":
        raise WikiError("not a linked wiki repository")
    try:
        repo, root = linked_wiki.resolve_root(row["root_path"], row["wiki_root"] or ".")
    except (OSError, linked_wiki.LinkedWikiError) as exc:
        raise WikiError(f"linked wiki is unavailable: {exc}") from exc
    return repo, root, row


def linked_paths(wiki_id: str) -> tuple[Path, Path]:
    """Public accessor for a linked wiki's ``(repo, wiki root)`` Paths — a thin
    wrapper over ``_linked_roots`` that drops the DB row. Callers outside this
    module use this instead of the private ``_linked_roots``."""
    repo, root, _row = _linked_roots(wiki_id)
    return repo, root


def probe_linked_wiki(wiki_id: str) -> dict:
    repo, root, row = _linked_roots(wiki_id)
    result = linked_wiki.probe(str(repo), _rel_to(repo, root))
    try:
        previous_recipe = _row_recipe(row)
        previous_trusted = set(json.loads(row["trusted_operations_json"] or "[]"))
    except json.JSONDecodeError:
        previous_recipe, previous_trusted = {}, set()
    previous = linked_wiki.normalize_operations(previous_recipe, repo)
    local_override = _json_object(row["local_override_json"] if "local_override_json" in row.keys() else None)
    current = linked_wiki.normalize_operations(_effective_recipe(result["manifest"], local_override), repo)
    trusted = sorted(
        name for name in previous_trusted
        if name in current and name in previous and current[name]["configHash"] == previous[name]["configHash"]
    )
    invalidated = sorted(previous_trusted - set(trusted))
    with _lock, _conn() as conn:
        conn.execute("""UPDATE wiki
                        SET recipe_json=?, trusted_operations_json=?, observed_revision=?, observed_at=?, last_error=?
                        WHERE id=?""",
                     (json.dumps(result["manifest"]), json.dumps(trusted), result.get("revision"), _now(),
                      result.get("manifestError"), wiki_id))
    return {**result, "invalidatedOperations": invalidated, "wiki": get_wiki(wiki_id)}


def linked_status(wiki_id: str) -> dict:
    repo, root, row = _linked_roots(wiki_id)
    operations = list_linked_operations(wiki_id)
    setup_done = row["setup_completed_at"] if "setup_completed_at" in row.keys() else None
    setup_pending = any(op.get("kind") == "setup" for op in operations) and not setup_done
    return {"wiki": _wiki_row(row), "repoPath": str(repo), "wikiRoot": _rel_to(repo, root),
            "instructions": linked_wiki.instructions(repo, root),
            "operations": operations,
            "trustTier": get_trust_tier(wiki_id),
            "setupPending": setup_pending,
            "lastSyncAt": (row["last_sync_at"] if "last_sync_at" in row.keys() else None),
            "currentRevision": linked_wiki.current_revision(repo)}


def _op_trusted(operation: dict, name: str, trusted: set[str]) -> bool:
    """A manual (``explicit_user``) operation runs only when a human clicks Run —
    or asks for it directly — so that click *is* the approval and it needs no
    separate trust grant.  The per-op trust gate (PRD-21 FR-16) exists for
    ``assistant``-mode operations the agent runs unattended; those still require
    an explicit Trust.  (The agent's own ``wiki_run_operation`` keeps its
    independent ``explicit_user_request`` guard, so this never lets the agent run
    a manual op autonomously.)"""
    return operation.get("mode") == "explicit_user" or name in trusted


def list_linked_operations(wiki_id: str) -> list[dict]:
    repo, _root, row = _linked_roots(wiki_id)
    recipe = _row_recipe(row)
    trusted = set(json.loads(row["trusted_operations_json"] or "[]"))
    local = _json_object(row["local_override_json"] if "local_override_json" in row.keys() else None)
    local_names = set((local.get("operations") or {}).keys())
    return [{**op, "trusted": _op_trusted(op, name, trusted), "source": "local" if name in local_names else "repo"}
            for name, op in linked_wiki.normalize_operations(recipe, repo).items()]


def upsert_local_operation(wiki_id: str, name: str, spec: dict) -> dict:
    """Create or replace a *local* operation (stored in ``local_override_json``,
    never written into the checkout).  The spec is validated by running it
    through ``normalize_operations`` (same rules as any repository operation);
    authoring or editing always drops the name from trust (PRD-21 FR-16) so the
    operator must re-approve the new definition before it can run."""
    repo, _root, row = _linked_roots(wiki_id)
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise WikiError("operation name must match [a-z][a-z0-9_-]{0,63}")
    if not isinstance(spec, dict):
        raise WikiError("operation spec must be an object")
    # Validate the raw spec in isolation; normalize_operations rejects invalid
    # argv/inputs/mode/cwd — we do not reimplement that here.
    if name not in linked_wiki.normalize_operations({"operations": {name: spec}}, repo):
        raise WikiError("operation definition is invalid (check argv, inputs, cwd, and mode)")
    local = _json_object(row["local_override_json"] if "local_override_json" in row.keys() else None)
    local_ops = dict(local.get("operations") or {})
    local_ops[name] = spec
    local["operations"] = local_ops
    trusted = [t for t in json.loads(row["trusted_operations_json"] or "[]") if t != name]
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET local_override_json=?, trusted_operations_json=? WHERE id=?",
                     (json.dumps(local), json.dumps(trusted), wiki_id))
    return linked_status(wiki_id)


def delete_local_operation(wiki_id: str, name: str) -> dict:
    """Remove a local operation (and any trust grant).  Repository-defined
    operations cannot be deleted — they live in the versioned manifest."""
    repo, _root, row = _linked_roots(wiki_id)
    local = _json_object(row["local_override_json"] if "local_override_json" in row.keys() else None)
    local_ops = dict(local.get("operations") or {})
    if name not in local_ops:
        source = _json_object(row["recipe_json"] if "recipe_json" in row.keys() else None)
        source_ops = source.get("operations") if isinstance(source.get("operations"), dict) else {}
        if name in source_ops:
            raise WikiError("repository-defined operations cannot be deleted; override or untrust it instead")
        raise WikiError("no such local operation")
    local_ops.pop(name, None)
    local["operations"] = local_ops
    trusted = [t for t in json.loads(row["trusted_operations_json"] or "[]") if t != name]
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET local_override_json=?, trusted_operations_json=? WHERE id=?",
                     (json.dumps(local), json.dumps(trusted), wiki_id))
    return linked_status(wiki_id)


def set_trusted_operations(wiki_id: str, names: list[str]) -> dict:
    repo, _root, row = _linked_roots(wiki_id)
    allowed = set(linked_wiki.normalize_operations(_row_recipe(row), repo))
    trusted = sorted(set(names or []) & allowed)
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET trusted_operations_json=? WHERE id=?", (json.dumps(trusted), wiki_id))
    return get_wiki(wiki_id) or {}


def linked_instructions(wiki_id: str, path: str = "") -> list[dict]:
    repo, root, _row = _linked_roots(wiki_id)
    try:
        return linked_wiki.instructions(repo, root, path)
    except linked_wiki.LinkedWikiError as exc:
        raise WikiError(str(exc)) from exc


def resolve_operation(wiki_id: str, name: str, *, require_trusted: bool = True) -> tuple[Path, dict]:
    """Refresh the recipe, then resolve a configured operation and enforce trust.

    Shared by the synchronous ``run_linked_operation`` and the async job runner
    (``storage/wiki_jobs``) so the recipe re-read + trust check (PRD-21 FR-16)
    live in exactly one place.  Re-reading first means a changed manifest has
    already dropped any no-longer-identical trust grant before we return."""
    probe_linked_wiki(wiki_id)
    repo, _root, row = _linked_roots(wiki_id)
    ops = linked_wiki.normalize_operations(_row_recipe(row), repo)
    operation = ops.get(name)
    if not operation:
        raise WikiError("no such configured repository operation")
    trusted = set(json.loads(row["trusted_operations_json"] or "[]"))
    if require_trusted and not _op_trusted(operation, name, trusted):
        raise WikiError("repository operation is not trusted; approve it in the Wiki settings first")
    return repo, operation


def mark_operation(wiki_id: str, name: str, error: str | None) -> None:
    """Record the last operation run + its error (None on success) on the wiki row."""
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET last_operation=?, last_error=?, observed_at=? WHERE id=?",
                     (name, error, _now(), wiki_id))


def _note_kind_success(wiki_id: str, operation_name: str) -> None:
    """After a successful operation, stamp the wiki by the operation's *kind*
    (setup → first setup_completed_at, sync → last_sync_at) and re-probe. Shared
    by ``run_linked_operation`` and the async job runner so the kind-based
    surfaces live in one place. MUST NOT be called while holding ``_lock`` — it
    locks internally (and re-probes)."""
    try:
        repo, _root, row = _linked_roots(wiki_id)
        ops = linked_wiki.normalize_operations(_row_recipe(row), repo)
        kind = (ops.get(operation_name) or {}).get("kind", "maintenance")
    except WikiError:
        return
    if kind == "setup":
        with _lock, _conn() as conn:
            conn.execute("UPDATE wiki SET setup_completed_at=COALESCE(setup_completed_at,?) WHERE id=?",
                         (_now(), wiki_id))
    elif kind == "sync":
        with _lock, _conn() as conn:
            conn.execute("UPDATE wiki SET last_sync_at=? WHERE id=?", (_now(), wiki_id))
    else:
        return
    probe_linked_wiki(wiki_id)


def run_linked_operation(wiki_id: str, name: str, inputs: dict | None = None, *, require_trusted: bool = True) -> dict:
    repo, operation = resolve_operation(wiki_id, name, require_trusted=require_trusted)
    try:
        result = linked_wiki.run_operation(repo, operation, inputs or {})
    except linked_wiki.LinkedWikiError as exc:
        raise WikiError(str(exc)) from exc
    mark_operation(wiki_id, name, None if result.get("exitCode") == 0 else result.get("output", "")[-1000:])
    if result.get("exitCode") == 0:
        _note_kind_success(wiki_id, name)
    return result


# --------------------------------------------------------------------------- #
# Wiki Repository jobs (PRD-21 FR-15) — the async runner persists here
# --------------------------------------------------------------------------- #

def _job_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "wikiId": r["wiki_id"], "operation": r["operation"], "status": r["status"],
            "argv": json.loads(r["argv_json"] or "[]"), "cwd": r["cwd"],
            "startedAt": r["started_at"], "finishedAt": r["finished_at"],
            "exitCode": r["exit_code"], "output": r["output"] or "", "error": r["error"]}


def record_job(job: dict) -> None:
    """Upsert a job snapshot (camelCase dict from the runner) into app.db."""
    with _lock, _conn() as conn:
        conn.execute("""INSERT OR REPLACE INTO wiki_repository_job
            (id,wiki_id,operation,status,argv_json,cwd,started_at,finished_at,exit_code,output,error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], job["wikiId"], job["operation"], job["status"], json.dumps(job.get("argv") or []),
             job.get("cwd"), job.get("startedAt"), job.get("finishedAt"), job.get("exitCode"),
             job.get("output"), job.get("error")))


def get_job(wiki_id: str, job_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM wiki_repository_job WHERE id=? AND wiki_id=?", (job_id, wiki_id)).fetchone()
    return _job_row(r) if r else None


def list_recent_jobs(wiki_id: str, limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM wiki_repository_job WHERE wiki_id=? ORDER BY started_at DESC LIMIT ?",
                            (wiki_id, max(1, min(int(limit or 20), 200)))).fetchall()
    return [_job_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# Read-only shell refusals (linked-wiki revamp §3) — a demand signal for a
# future writable tier; never a hard failure path.
# --------------------------------------------------------------------------- #

def log_shell_refusal(wiki_id: str, command: str, reason: str) -> None:
    """Best-effort: record one refused read-only shell command for a linked wiki.
    Never raises (a logging failure must not break a tool call); prunes to the
    newest 500 rows per wiki."""
    try:
        with _lock, _conn() as conn:
            conn.execute(
                "INSERT INTO wiki_shell_refusal (wiki_id, command, reason, created_at) VALUES (?,?,?,?)",
                (wiki_id, (command or "")[:2000], (reason or "")[:2000], _now()))
            conn.execute(
                """DELETE FROM wiki_shell_refusal WHERE wiki_id=? AND id NOT IN
                   (SELECT id FROM wiki_shell_refusal WHERE wiki_id=? ORDER BY id DESC LIMIT 500)""",
                (wiki_id, wiki_id))
    except Exception as exc:  # noqa: BLE001 - never break a tool call over logging
        logger.warning("log_shell_refusal failed for %s: %s", wiki_id, exc)


def list_shell_refusals(wiki_id: str, limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wiki_shell_refusal WHERE wiki_id=? ORDER BY id DESC LIMIT ?",
            (wiki_id, max(1, min(int(limit or 100), 500)))).fetchall()
    return [{"id": r["id"], "wikiId": r["wiki_id"], "command": r["command"],
             "reason": r["reason"], "createdAt": r["created_at"]} for r in rows]


# --------------------------------------------------------------------------- #
# Trust tier — set ONLY by proposal execution (the agent can never self-escalate)
# --------------------------------------------------------------------------- #

def get_trust_tier(wiki_id: str) -> str:
    row = _raw_wiki(wiki_id)
    if not row:
        raise WikiError("no such wiki")
    return (row["trust_tier"] if "trust_tier" in row.keys() and row["trust_tier"] else "readonly")


def set_trust_tier(wiki_id: str, tier: str) -> dict:
    """Set a linked wiki's trust tier. **Only ``approve_proposal`` execution calls
    this** — no agent tool wires to it, so the agent can never escalate its own
    tier. The user decides the tier by approving a ``trust_tier`` proposal."""
    t = _valid_trust_tier(tier)
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET trust_tier=? WHERE id=?", (t, wiki_id))
    return get_wiki(wiki_id) or {}


# --------------------------------------------------------------------------- #
# Unified approval queue (linked-wiki revamp §5) — persisted proposals
# --------------------------------------------------------------------------- #

_PROPOSAL_KINDS = {"link", "operations", "trust_tier"}


class _ProposalExecError(Exception):
    """Raised inside proposal execution to mark a *partial* failure while
    preserving what already happened (e.g. a wiki that was created before a
    later op upsert failed). There is no rollback — see §5."""

    def __init__(self, partial: dict, message: str) -> None:
        super().__init__(message)
        self.partial = partial


def _proposal_row(r: sqlite3.Row) -> dict:
    payload = _json_object(r["payload_json"]) if r["payload_json"] else {}
    result = _json_object(r["result_json"]) if r["result_json"] else None
    execution = None
    if result and result.get("jobIds"):
        execution = {"jobId": result["jobIds"][0]}
    return {"id": r["id"], "kind": r["kind"], "status": r["status"], "wikiId": r["wiki_id"],
            "createdAt": r["created_at"], "decidedAt": r["decided_at"], "executedAt": r["executed_at"],
            "note": r["user_note"], "payload": payload, "result": result, "execution": execution}


def _raw_proposal(proposal_id: str):
    with _conn() as conn:
        return conn.execute("SELECT * FROM wiki_proposal WHERE id=?", (proposal_id,)).fetchone()


def create_proposal(kind: str, wiki_id: str | None = None, payload: dict | None = None) -> dict:
    if kind not in _PROPOSAL_KINDS:
        raise WikiError(f"invalid proposal kind {kind!r}")
    pid, now = uuid.uuid4().hex[:12], _now()
    with _lock, _conn() as conn:
        conn.execute("""INSERT INTO wiki_proposal (id,kind,wiki_id,payload_json,status,created_at)
                        VALUES (?,?,?,?,?,?)""",
                     (pid, kind, wiki_id, json.dumps(payload or {}), "awaiting_approval", now))
    return get_proposal(pid)


def get_proposal(proposal_id: str) -> dict | None:
    r = _raw_proposal(proposal_id)
    return _proposal_row(r) if r else None


def list_proposals(status: str = "", wiki_id: str | None = None) -> list[dict]:
    status = (status or "").strip()
    if status == "pending":                       # UI alias
        status = "awaiting_approval"
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if wiki_id:
        clauses.append("wiki_id=?")
        params.append(wiki_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as conn:
        rows = conn.execute(f"SELECT * FROM wiki_proposal{where} ORDER BY created_at DESC", params).fetchall()
    return [_proposal_row(r) for r in rows]


def reject_proposal(proposal_id: str, note: str = "") -> dict:
    row = _raw_proposal(proposal_id)
    if not row:
        raise WikiError("no such proposal")
    if row["status"] != "awaiting_approval":
        raise WikiError("proposal is already resolved")
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki_proposal SET status='rejected', decided_at=?, user_note=? WHERE id=?",
                     (_now(), (note or "").strip() or None, proposal_id))
    return get_proposal(proposal_id)


def _finish_proposal(proposal_id: str, status: str, wiki_id: str | None, result: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute("""UPDATE wiki_proposal SET status=?, executed_at=?, wiki_id=COALESCE(?,wiki_id),
                        result_json=? WHERE id=?""",
                     (status, _now(), wiki_id, json.dumps(result), proposal_id))


def _op_to_spec(op: dict) -> dict:
    # "kind" MUST be carried through — dropping it makes normalize_operations
    # default a discovered setup/sync op back to "maintenance" on link, which
    # also breaks the kind-driven Sync-now / Run-setup buttons.
    return {k: op[k] for k in ("description", "argv", "cwd", "inputs", "mode", "kind", "timeoutSeconds", "outputLimitBytes")
            if k in op}


async def _execute_link(payload: dict, overrides: dict) -> dict:
    tier = _valid_trust_tier(overrides.get("trustTier") or payload.get("requestedTrustTier") or "repo-tooling")
    operations = payload.get("operations") or []
    checked = overrides.get("checkedOps")
    if checked is not None:
        cset = set(checked)
        approved = [op for op in operations if op.get("name") in cset]
    else:
        approved = list(operations)
    linked = create_linked_wiki(payload.get("name") or "", payload["repoPath"], payload.get("wikiRoot") or ".",
                                payload.get("description") or "", trust_tier=tier)
    wiki_id = linked["id"]
    result = {"wikiId": wiki_id, "trusted": [], "skipped": [], "setupOperation": None}
    # Attach the new wiki to the proposing agent (tool-originated proposals carry
    # proposedByAgentId): the agent's wiki tools only see *attached* wikis, so
    # without this a non-default proposer could never use the link it asked for
    # (create_linked_wiki auto-attaches only the seeded Assistant). Best-effort —
    # a deleted agent must not fail the approval.
    proposer = (payload.get("proposedByAgentId") or "").strip()
    if proposer:
        try:
            rec = agent_store.get_agent(proposer)
            if rec is not None:
                if wiki_id not in (rec.get("wikis") or []):
                    agent_store.update_agent(proposer, {"wikis": [*(rec.get("wikis") or []), wiki_id]})
                result["attachedToAgent"] = proposer
        except Exception:  # noqa: BLE001
            logger.warning("link proposal: could not attach wiki %s to agent %s",
                           wiki_id, proposer, exc_info=True)
    try:
        for op in approved:                       # upsert first — upsert drops trust
            name = op.get("name")
            try:
                upsert_local_operation(wiki_id, name, _op_to_spec(op))
            except WikiError:
                result["skipped"].append(name)
        # THEN trust the approved assistant-mode ops (trust must be last).
        assistant = sorted({op.get("name") for op in approved
                            if op.get("mode") == "assistant" and op.get("name") not in result["skipped"]})
        if assistant:
            set_trusted_operations(wiki_id, assistant)
            result["trusted"] = assistant
        result["setupOperation"] = next((op.get("name") for op in approved if op.get("kind") == "setup"), None)
    except Exception as exc:                       # noqa: BLE001 - keep the created wiki, mark failed
        raise _ProposalExecError(result, str(exc)) from exc
    return result


async def _execute_operations(wiki_id: str, payload: dict, overrides: dict) -> dict:
    if not wiki_id:
        raise WikiError("operations proposal has no target wiki")
    from storage import wiki_jobs
    changes = payload.get("changes") or []
    run_once = payload.get("runOnce") or []
    checked = overrides.get("checkedOps")
    tier = overrides.get("trustTier") or payload.get("trustTier")
    result: dict = {"applied": [], "removed": [], "trusted": [], "jobIds": []}
    for ch in changes:
        action, name = ch.get("action"), ch.get("name")
        if action in ("add", "edit"):
            upsert_local_operation(wiki_id, name, ch.get("spec") or {})
            result["applied"].append(name)
        elif action == "remove":
            try:
                delete_local_operation(wiki_id, name)
                result["removed"].append(name)
            except WikiError:
                pass
    assistant_adds = [ch["name"] for ch in changes
                      if ch.get("action") in ("add", "edit") and (ch.get("spec") or {}).get("mode") == "assistant"]
    if checked is not None:
        assistant_adds = [n for n in assistant_adds if n in set(checked)]
    if assistant_adds:
        current = [op["name"] for op in list_linked_operations(wiki_id) if op.get("trusted")]
        set_trusted_operations(wiki_id, sorted(set(current) | set(assistant_adds)))
        result["trusted"] = sorted(set(assistant_adds))
    if tier:
        set_trust_tier(wiki_id, tier)
        result["trustTier"] = _valid_trust_tier(tier)
    for ro in run_once:
        job = await wiki_jobs.start_adhoc(wiki_id, ro.get("argv") or [], ro.get("reason") or "")
        result["jobIds"].append(job.id)
    return result


async def _execute_trust_tier(wiki_id: str, payload: dict, overrides: dict) -> dict:
    if not wiki_id:
        raise WikiError("trust_tier proposal has no target wiki")
    tier = _valid_trust_tier(overrides.get("trustTier") or payload.get("toTier"))
    set_trust_tier(wiki_id, tier)
    return {"wikiId": wiki_id, "trustTier": tier}


async def approve_proposal(proposal_id: str, overrides: dict | None = None) -> dict:
    """Approve a proposal (UI-only — no tool reaches this) then execute it
    synchronously: ``awaiting_approval → approved → executed`` (with result) or
    ``failed`` (with error + any partial result). Raises ``WikiError`` if the
    proposal is missing or not awaiting approval."""
    overrides = overrides or {}
    row = _raw_proposal(proposal_id)
    if not row:
        raise WikiError("no such proposal")
    if row["status"] != "awaiting_approval":
        raise WikiError("proposal is not awaiting approval")
    note = (overrides.get("note") or "").strip() or None
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki_proposal SET status='approved', decided_at=?, user_note=COALESCE(?,user_note) WHERE id=?",
                     (_now(), note, proposal_id))
    kind, payload = row["kind"], _json_object(row["payload_json"])
    try:
        if kind == "link":
            result = await _execute_link(payload, overrides)
        elif kind == "operations":
            result = await _execute_operations(row["wiki_id"], payload, overrides)
        elif kind == "trust_tier":
            result = await _execute_trust_tier(row["wiki_id"], payload, overrides)
        else:
            raise WikiError(f"unknown proposal kind {kind!r}")
    except _ProposalExecError as exc:
        partial = {**exc.partial, "error": str(exc)}
        _finish_proposal(proposal_id, "failed", partial.get("wikiId") or row["wiki_id"], partial)
        return get_proposal(proposal_id)
    except Exception as exc:                       # noqa: BLE001 - never crash approve; record failure
        logger.warning("proposal %s (%s) failed: %s", proposal_id, kind, exc)
        _finish_proposal(proposal_id, "failed", row["wiki_id"], {"error": str(exc)})
        return get_proposal(proposal_id)
    _finish_proposal(proposal_id, "executed", result.get("wikiId") or row["wiki_id"], result)
    return get_proposal(proposal_id)


def list_wikis() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM wiki ORDER BY created_at DESC").fetchall()
    return [_wiki_row(r) for r in rows]


def get_wiki(wiki_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM wiki WHERE id=?", (wiki_id,)).fetchone()
    return _wiki_row(r) if r else None


def delete_wiki(wiki_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM wiki WHERE id=?", (wiki_id,))
    base = _bundle(wiki_id)
    if base.exists() and base.parent == _wikis_root():   # confined: never rm outside wikis/
        shutil.rmtree(base, ignore_errors=True)
    return cur.rowcount > 0


def _touch_ingested(wiki_id: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE wiki SET ingested_at=? WHERE id=?", (_now(), wiki_id))


# --------------------------------------------------------------------------- #
# Pages (files on disk)
# --------------------------------------------------------------------------- #

def _page_meta(path: Path, base: Path) -> dict:
    rel = str(path.relative_to(base))
    meta, body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    return {
        "path": rel,
        "title": _meta_str(meta.get("title")).strip() or rel,
        "type": _meta_str(meta.get("type")).strip(),
        "description": _meta_str(meta.get("description")).strip(),
    }


def list_pages(wiki_id: str) -> list[dict]:
    if is_linked(wiki_id):
        _repo, root, _row = _linked_roots(wiki_id)
        return linked_wiki.list_pages(root)
    base = _bundle(wiki_id)
    return [_page_meta(p, base) for p in _page_files(wiki_id) if p.name not in _RESERVED]


def list_pages_page(wiki_id: str, prefix: str = "", offset: int = 0, limit: int = 50) -> dict:
    """A paginated ``{items,total,hasMore}`` slice of the page list.  Linked
    wikis use the light filename-stem listing (no per-file heading read) so a
    repository with thousands of pages stays cheap to browse."""
    if is_linked(wiki_id):
        _repo, root, _row = _linked_roots(wiki_id)
        return linked_wiki.list_pages_page(root, prefix, offset, limit)
    pages = list_pages(wiki_id)
    pref = (prefix or "").strip().lower()
    if pref:
        pages = [p for p in pages if pref in p["path"].lower()]
    total = len(pages)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), 500))
    window = pages[offset:offset + limit]
    return {"items": [{"path": p["path"], "title": p["title"]} for p in window],
            "total": total, "hasMore": offset + limit < total}


def read_page(wiki_id: str, rel: str) -> str | None:
    if is_linked(wiki_id):
        _repo, root, _row = _linked_roots(wiki_id)
        try:
            return linked_wiki.read_page(root, rel)
        except linked_wiki.LinkedWikiError as exc:
            raise WikiError(str(exc)) from exc
    base = _bundle(wiki_id)
    p = _safe(base, rel)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def write_page(wiki_id: str, rel: str, content: str) -> dict:
    """Create/overwrite a page (confined to the bundle), forcing OKF conformance,
    and append a line to ``log.md``. Returns ``{path, bytes}``."""
    if is_linked(wiki_id):
        raise WikiError("linked wiki repositories are read-only")
    base = _bundle(wiki_id)
    rel = (rel or "").strip()
    if not rel:
        raise WikiError("a page path is required")
    if not rel.endswith(".md"):
        rel += ".md"
    p = _safe(base, rel)
    if p.name in _RESERVED:                       # index.md/log.md are managed, not concept pages
        raise WikiError(f"{p.name} is a reserved file — write a concept page instead")
    p.parent.mkdir(parents=True, exist_ok=True)
    text = ensure_okf(content or "")
    p.write_text(text, encoding="utf-8")
    _append_log(base, f"updated {p.relative_to(base)}")
    _touch_ingested(wiki_id)
    return {"path": str(p.relative_to(base)), "bytes": len(text.encode("utf-8"))}


def _append_log(base: Path, entry: str) -> None:
    log = base / LOG_FILE
    prev = log.read_text(encoding="utf-8", errors="replace") if log.exists() else "# Log\n"
    log.write_text(f"{prev.rstrip()}\n\n### {_now()}\n- {entry}\n", encoding="utf-8")


def read_log(wiki_id: str) -> str:
    if is_linked(wiki_id):
        return ""
    base = _bundle(wiki_id)
    log = base / LOG_FILE
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


# --------------------------------------------------------------------------- #
# Search — pure-Python keyword scan (files are the single source of truth)
# --------------------------------------------------------------------------- #

def _snippet(body: str, terms: list[str], width: int = 180) -> str:
    low = body.lower()
    pos = min((low.find(t) for t in terms if t in low), default=-1)
    if pos < 0:
        return re.sub(r"\s+", " ", body).strip()[:width]
    start = max(0, pos - width // 3)
    return ("…" if start else "") + re.sub(r"\s+", " ", body[start:start + width]).strip() + "…"


def search_pages(wiki_id: str, query: str, k: int = 5) -> list[dict]:
    if is_linked(wiki_id):
        _repo, root, row = _linked_roots(wiki_id)
        # A trusted repo search is deliberately opt-in.  For M1 plain-text
        # output is returned as one cited result; repositories can evolve to a
        # structured JSON convention without changing the safety boundary.
        # Search can execute an assistant-mode repository command, so refresh
        # the recipe first and never retain a trust decision across a change.
        probe_linked_wiki(wiki_id)
        operations = {op["name"]: op for op in list_linked_operations(wiki_id)}
        search = operations.get("search")
        if search and search.get("trusted"):
            result = run_linked_operation(wiki_id, "search", {"query": query})
            output = (result.get("output") or "").strip()
            if result.get("exitCode") == 0 and output:
                try:
                    parsed = json.loads(output)
                    candidates = parsed.get("results", parsed) if isinstance(parsed, dict) else parsed
                    if isinstance(candidates, list):
                        out = []
                        for item in candidates[:max(1, min(int(k or 5), 20))]:
                            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                                continue
                            try:
                                linked_wiki.read_page(root, item["path"])
                            except linked_wiki.LinkedWikiError:
                                continue
                            if linked_wiki.read_page(root, item["path"]) is None:
                                continue
                            out.append({"path": item["path"], "title": str(item.get("title") or item["path"]),
                                        "score": item.get("score", 1), "snippet": str(item.get("snippet") or "")})
                        if out:
                            return out
                except json.JSONDecodeError:
                    pass
                return [{"path": "repository-search", "title": "Repository search", "score": 1,
                         "snippet": output[:2000]}]
        return linked_wiki.direct_search(root, query, k)
    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return []
    base = _bundle(wiki_id)
    out: list[dict] = []
    for p in _page_files(wiki_id):
        if p.name in _RESERVED:
            continue
        meta, body = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        title = _meta_str(meta.get("title")).strip() or str(p.relative_to(base))
        desc = _meta_str(meta.get("description"))
        hay = f"{title}\n{desc}\n{body}".lower()
        # term frequency, with a title/description match weighted higher
        score = sum(hay.count(t) for t in terms) + 3 * sum(t in f"{title} {desc}".lower() for t in terms)
        if score:
            out.append({"path": str(p.relative_to(base)), "title": title,
                        "score": score, "snippet": _snippet(body, terms)})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:max(1, int(k))]


# --------------------------------------------------------------------------- #
# Delete a page
# --------------------------------------------------------------------------- #

def delete_page(wiki_id: str, rel: str) -> bool:
    if is_linked(wiki_id):
        raise WikiError("linked wiki repositories are read-only")
    base = _bundle(wiki_id)
    p = _safe(base, rel)
    if p.name in _RESERVED:
        raise WikiError(f"{p.name} is a reserved file and can't be deleted")
    if not p.is_file():
        return False
    p.unlink()
    _append_log(base, f"deleted {p.relative_to(base)}")
    _touch_ingested(wiki_id)
    return True


# --------------------------------------------------------------------------- #
# Link graph + lint (PRD-13 M3) — deterministic health checks over the files
# --------------------------------------------------------------------------- #

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _resolve_link(raw: str, from_rel: str, *, root_relative: bool) -> str | None:
    """Normalize a link target to a bundle-relative ``foo/bar.md`` path, or None
    if it's external/an asset. ``[[wikilinks]]`` are root-relative; markdown links
    resolve against the linking page's directory (unless they start with ``/``)."""
    t = (raw or "").strip().split("#", 1)[0].strip()
    if not t or t.startswith(("http://", "https://", "mailto:", "#")):
        return None
    ext = PurePosixPath(t).suffix.lower()
    if ext and ext != ".md":          # an image/asset link, not a page
        return None
    if t.startswith("/"):
        base, t = "", t[1:]
    elif root_relative:
        base = ""
    else:
        parent = str(PurePosixPath(from_rel).parent)
        base = "" if parent == "." else parent
    parts: list[str] = []
    for part in (PurePosixPath(base) / t).parts if base else PurePosixPath(t).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    norm = "/".join(parts)
    if not norm:
        return None
    return norm if norm.endswith(".md") else norm + ".md"


def _links_from(rel: str, text: str) -> list[str]:
    body = split_frontmatter(text)[1]
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(body):
        t = _resolve_link(m.group(1), rel, root_relative=True)
        if t:
            out.append(t)
    for m in _MDLINK_RE.finditer(body):
        t = _resolve_link(m.group(1), rel, root_relative=False)
        if t:
            out.append(t)
    return out


def link_graph(wiki_id: str) -> dict:
    """Nodes = concept pages; edges = resolved cross-links (``resolved`` flags
    whether the target page exists)."""
    base = _bundle(wiki_id)
    files = _page_files(wiki_id)
    exists = {str(p.relative_to(base)) for p in files}
    nodes = [str(p.relative_to(base)) for p in files if p.name not in _RESERVED]
    edges: list[dict] = []
    for p in files:
        if p.name in _RESERVED:
            continue
        rel = str(p.relative_to(base))
        seen: set[str] = set()
        for t in _links_from(rel, p.read_text(encoding="utf-8", errors="replace")):
            if t in seen:
                continue
            seen.add(t)
            edges.append({"from": rel, "to": t, "resolved": t in exists})
    return {"nodes": nodes, "edges": edges}


def _age_days(ts: str, now: datetime) -> int | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def lint(wiki_id: str, stale_days: int = 90) -> dict:
    """Deterministic health report over the bundle's files: pages nothing links
    to (orphans), links whose target is missing (broken), and pages whose
    ``timestamp`` is older than ``stale_days`` (stale). Contradiction/coverage
    analysis is left to the agent (it can `wiki_read` the flagged pages)."""
    base = _bundle(wiki_id)
    files = _page_files(wiki_id)
    exists = {str(p.relative_to(base)) for p in files}
    concept = [p for p in files if p.name not in _RESERVED]
    linked: set[str] = set()
    broken: list[dict] = []
    for p in concept:                       # scan links from concept pages only
        rel = str(p.relative_to(base))      # (log.md's history entries aren't links)
        for t in _links_from(rel, p.read_text(encoding="utf-8", errors="replace")):
            linked.add(t)
            if t not in exists:
                broken.append({"from": rel, "to": t})
    now = datetime.now(timezone.utc)
    orphans: list[str] = []
    stale: list[dict] = []
    for p in concept:
        rel = str(p.relative_to(base))
        if rel not in linked:
            orphans.append(rel)
        meta, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        age = _age_days(_meta_str(meta.get("timestamp")).strip(), now)
        if age is not None and age > stale_days:
            stale.append({"path": rel, "timestamp": _meta_str(meta.get("timestamp")), "ageDays": age})
    return {
        "orphans": sorted(orphans),
        "brokenLinks": broken,
        "stale": stale,
        "pageCount": len(concept),
        "staleDays": stale_days,
    }


# --------------------------------------------------------------------------- #
# Export / import — the bundle is just files, so portability is a zip (PRD-13 M4)
# --------------------------------------------------------------------------- #

def export_bundle(wiki_id: str) -> bytes:
    """Zip the whole OKF bundle (pages + index.md + log.md) for portability."""
    base = _bundle(wiki_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(base).as_posix())
    return buf.getvalue()


def import_bundle(name: str, data: bytes) -> dict:
    """Create a new wiki from an exported bundle zip. Concept pages are forced
    OKF-conformant on the way in; paths are confined (zip-slip safe)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise WikiError(f"not a valid bundle zip: {exc}") from exc
    members = [m for m in zf.namelist()
               if m.endswith(".md") and not m.startswith("/") and ".." not in m.split("/")]
    if not members:
        raise WikiError("no .md pages found in the bundle")
    w = create_wiki(name or "Imported wiki")
    base = _bundle(w["id"])
    for m in members:
        target = _safe(base, m)                 # confinement (zip-slip guard)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = zf.read(m).decode("utf-8", errors="replace")
        target.write_text(text if PurePosixPath(m).name in _RESERVED else ensure_okf(text),
                          encoding="utf-8")
    _touch_ingested(w["id"])
    return get_wiki(w["id"])


_DOCUMENT_EXTS = {".md", ".markdown", ".txt", ".csv", ".docx", ".pptx", ".xlsx"}


def import_document(name: str, filename: str, data: bytes) -> dict:
    """Convert a text or modern Office document into searchable wiki pages."""
    ext = Path(filename).suffix.lower()
    if ext not in _DOCUMENT_EXTS:
        raise WikiError("supported files: .zip, .md, .txt, .csv, .docx, .pptx, and .xlsx")
    title = (name or Path(filename).stem or "Imported document").strip()
    try:
        if ext in {".md", ".markdown", ".txt"}:
            pages = [(title, data.decode("utf-8", errors="replace"))]
        elif ext == ".csv":
            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
            pages = [(title, _markdown_table(rows))]
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                pages = _office_pages(archive, ext, title)
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise WikiError(f"could not read {ext} document: {exc}") from exc
    if not pages or not any(body.strip() for _, body in pages):
        raise WikiError("no readable text found in the document")

    wiki = create_wiki(title, f"Imported locally from {filename}")
    for index, (page_title, body) in enumerate(pages, 1):
        if not body.strip():
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", page_title.lower()).strip("-") or f"page-{index}"
        write_page(wiki["id"], f"{index:03d}-{slug}.md", f"# {page_title}\n\n{body.strip()}\n")
    return get_wiki(wiki["id"])


def _office_pages(archive: zipfile.ZipFile, ext: str, title: str) -> list[tuple[str, str]]:
    if ext == ".docx":
        root = ET.fromstring(archive.read("word/document.xml"))
        paragraphs = ["".join(node.itertext()).strip() for node in root.iter() if node.tag.endswith("}p")]
        return [(title, "\n\n".join(p for p in paragraphs if p))]
    if ext == ".pptx":
        slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"\d+", Path(n).stem).group()))
        return [(f"{title} - Slide {i}", "\n\n".join(t.text or "" for t in ET.fromstring(archive.read(path)).iter()
                                                        if t.tag.endswith("}t")))
                for i, path in enumerate(slides, 1)]

    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        shared = ["".join(node.itertext()) for node in ET.fromstring(archive.read("xl/sharedStrings.xml"))
                  if node.tag.endswith("}si")]
    sheets = sorted((n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                    key=lambda n: int(re.search(r"\d+", Path(n).stem).group()))
    pages = []
    for i, path in enumerate(sheets, 1):
        rows = []
        for row in (node for node in ET.fromstring(archive.read(path)).iter() if node.tag.endswith("}row")):
            values = []
            for cell in (node for node in row if node.tag.endswith("}c")):
                value = next((node.text or "" for node in cell.iter() if node.tag.endswith("}v")), "")
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
                values.append(value)
            rows.append(values)
        pages.append((f"{title} - Sheet {i}", _markdown_table(rows)))
    return pages


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(map(len, rows))
    clean = [[str(v).replace("|", "\\|").replace("\n", " ") for v in row] + [""] * (width - len(row))
             for row in rows]
    header, *body = clean
    return "\n".join([f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * width)} |",
                      *(f"| {' | '.join(row)} |" for row in body)])
