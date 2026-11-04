"""Local user accounts + auth sessions for optional Team mode (PRD-07).

Local-first, zero new deps: passwords are hashed with the stdlib
``hashlib.pbkdf2_hmac`` (per-user random salt); session tokens are random opaque
strings of which only a SHA-256 **hash** is stored. Nothing here makes a network
call — auth stays as offline as the rest of Virtual Worker.

Roles: ``owner`` > ``admin`` > ``member`` > ``viewer``. The **first** user created
becomes the ``owner`` and bootstraps Team mode (see ``routes/auth.py``).

This module is dormant in solo mode: with zero users the studio never consults it
on the request path, so default localhost behavior is byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from config import db_path

_lock = threading.Lock()

ROLES = ("owner", "admin", "member", "viewer")
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}

# pbkdf2 work factor. 200k SHA-256 rounds is a sane local default (no GPU farm
# threat model here, but cheap enough to be brute-force resistant on a laptop).
_PBKDF2_ITERATIONS = 200_000

# Default session lifetime; the Team settings block can override this later.
_DEFAULT_SESSION_DAYS = 7


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active',
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash  TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Password hashing (stdlib pbkdf2, constant-time verify)
# --------------------------------------------------------------------------- #

def _hash_password(password: str, *, salt: bytes | None = None,
                   iterations: int = _PBKDF2_ITERATIONS) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def _check_password(password: str, stored: str) -> bool:
    """Constant-time password check. Returns False on any malformed record."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk.hex(), hash_hex)


def _public(row: sqlite3.Row | dict) -> dict:
    """Project a row to the camelCase shape the API returns — never the hash."""
    r = dict(row)
    return {
        "id": r["id"],
        "username": r["username"],
        "role": r["role"],
        "status": r.get("status", "active"),
        "createdAt": r["created_at"],
        "lastLoginAt": r.get("last_login_at"),
    }


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username: str, password: str, role: str | None = None) -> dict:
    """Create a user. The **first** user is always the ``owner`` regardless of the
    requested role; subsequent users default to ``member``.

    Raises ``ValueError`` on a blank/duplicate username, blank password, or an
    invalid role.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    with _lock, _conn() as conn:
        first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        effective_role = "owner" if first else (role or "member")
        if effective_role not in ROLES:
            raise ValueError(f"invalid role: {effective_role!r}")
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            raise ValueError("username already taken")
        uid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (uid, username, _hash_password(password), effective_role, _now().isoformat()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return _public(row)


def get_user(user_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


def get_by_username(username: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
        ).fetchone()
    return _public(row) if row else None


def list_users() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [_public(r) for r in rows]


def verify_password(username: str, password: str) -> dict | None:
    """Return the public user dict on a correct, active credential, else None.

    Always runs a pbkdf2 hash (even for unknown users / disabled accounts) so the
    response time doesn't leak whether the username exists.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
        ).fetchone()
    # Decoy hash to keep timing roughly constant for unknown usernames.
    stored = row["password_hash"] if row else _hash_password("\0decoy\0")
    ok = _check_password(password or "", stored)
    if not row or not ok or (row["status"] or "active") != "active":
        return None
    return _public(row)


def update_role(user_id: str, role: str) -> dict | None:
    if role not in ROLES:
        raise ValueError(f"invalid role: {role!r}")
    with _lock, _conn() as conn:
        cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row)


def update_status(user_id: str, status: str) -> dict | None:
    if status not in ("active", "disabled"):
        raise ValueError(f"invalid status: {status!r}")
    with _lock, _conn() as conn:
        cur = conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row)


def delete_user(user_id: str) -> bool:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def set_password(user_id: str, password: str) -> bool:
    if not password:
        raise ValueError("password is required")
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(password), user_id),
        )
        # New password invalidates existing sessions.
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Sessions (random opaque token; only its hash is persisted)
# --------------------------------------------------------------------------- #

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(user_id: str, *, ttl_days: int = _DEFAULT_SESSION_DAYS) -> str:
    """Create a session for ``user_id`` and return the **raw** token (caller signs
    it into the cookie). Also records last_login_at."""
    raw = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=ttl_days)
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(raw), user_id, now.isoformat(), expires.isoformat()),
        )
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), user_id)
        )
    return raw


def resolve_session(raw: str) -> dict | None:
    """Return the public user for a valid, unexpired session token, else None.

    A revoked user, a disabled account, or an expired/unknown token all resolve to
    None — so disabling a user instantly invalidates their live sessions.
    """
    if not raw:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT s.expires_at AS expires_at, u.* FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_hash_token(raw),),
        ).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < _now():
            destroy_session(raw)
            return None
    except (ValueError, TypeError):
        return None
    if (row["status"] or "active") != "active":
        return None
    return _public(row)


def destroy_session(raw: str) -> None:
    if not raw:
        return
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_hash_token(raw),))
