"""Team mode — accounts, login/session, the /api auth chokepoint, role gates
(PRD-07). Proves solo mode is unchanged when Team mode is off.
"""

import pytest
from fastapi.testclient import TestClient

import config
import team
import storage.token_store as ts
from app import app
from storage import user_store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate config.APP_HOME -> tmp_path so each test gets a fresh studio."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    ts._cache = None
    ts._last_used_flush.clear()
    yield tmp_path


def _client() -> TestClient:
    # Plain instantiation (no `with`) skips the app's startup/shutdown lifespan
    # (no llama autostart/scheduler) while still routing through the middleware.
    return TestClient(app)


def _bootstrap_owner(c: TestClient):
    r = c.post("/api/auth/register", json={"username": "owner", "password": "pw-owner"})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Solo mode (default OFF) — behavior must be byte-for-byte unchanged
# --------------------------------------------------------------------------- #

def test_solo_mode_off_by_default(env):
    assert team.is_enabled() is False
    c = _client()
    # An existing /api route works with no auth at all.
    assert c.get("/api/tokens").status_code == 200
    # /me reports solo: no team, no user, no login wall.
    me = c.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"teamMode": False, "user": None}


# --------------------------------------------------------------------------- #
# Bootstrap + the trust-boundary flip
# --------------------------------------------------------------------------- #

def test_first_register_is_owner_and_enables_team(env):
    c = _client()
    body = _bootstrap_owner(c)
    assert body["teamMode"] is True
    assert body["user"]["role"] == "owner"
    assert team.is_enabled() is True
    # A session cookie was set, so this same client is authenticated.
    assert team.COOKIE_NAME in c.cookies
    me = c.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "owner"


def test_api_requires_auth_once_team_enabled(env):
    _bootstrap_owner(_client())  # enable team mode
    # A brand-new client with no cookie is rejected on /api.
    anon = _client()
    assert anon.get("/api/tokens").status_code == 401
    assert anon.get("/api/auth/me").status_code == 401
    # health + login stay reachable.
    assert anon.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #

def test_login_logout_and_wrong_password(env):
    _bootstrap_owner(_client())
    c = _client()  # fresh, unauthenticated
    # wrong password -> generic 401 (constant-time compare, no user/pwd leak)
    bad = c.post("/api/auth/login", json={"username": "owner", "password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "invalid username or password"
    # unknown user -> same generic 401
    unk = c.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert unk.status_code == 401
    # correct -> session + /me works
    ok = c.post("/api/auth/login", json={"username": "owner", "password": "pw-owner"})
    assert ok.status_code == 200
    assert c.get("/api/auth/me").json()["user"]["username"] == "owner"
    # logout clears the session -> /api locked again
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Role gates (server-side, not UI-only)
# --------------------------------------------------------------------------- #

def _login(username, password) -> TestClient:
    c = _client()
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_role_gated_user_management(env):
    owner = _client()
    _bootstrap_owner(owner)
    # Owner invites a member and a viewer.
    assert owner.post("/api/users", json={"username": "mem", "password": "pw-mem", "role": "member"}).status_code == 200
    assert owner.post("/api/users", json={"username": "vw", "password": "pw-vw", "role": "viewer"}).status_code == 200

    member = _login("mem", "pw-mem")
    viewer = _login("vw", "pw-vw")

    # Members/viewers cannot manage users.
    assert member.get("/api/users").status_code == 403
    assert member.post("/api/users", json={"username": "x", "password": "y"}).status_code == 403
    assert viewer.post("/api/users", json={"username": "x", "password": "y"}).status_code == 403

    # Owner can list and manage.
    listed = owner.get("/api/users")
    assert listed.status_code == 200
    assert {u["username"] for u in listed.json()["users"]} == {"owner", "mem", "vw"}

    # Owner promotes the member to admin, then removes the viewer.
    mem_id = next(u["id"] for u in listed.json()["users"] if u["username"] == "mem")
    vw_id = next(u["id"] for u in listed.json()["users"] if u["username"] == "vw")
    assert owner.patch(f"/api/users/{mem_id}", json={"role": "admin"}).status_code == 200
    assert owner.delete(f"/api/users/{vw_id}").status_code == 200


def test_disabled_user_session_invalidated(env):
    owner = _client()
    _bootstrap_owner(owner)
    owner.post("/api/users", json={"username": "mem", "password": "pw-mem", "role": "member"})
    member = _login("mem", "pw-mem")
    assert member.get("/api/auth/me").status_code == 200
    # Disabling the member instantly invalidates their live session.
    user_store.update_status(user_store.get_by_username("mem")["id"], "disabled")
    assert member.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Cookie integrity
# --------------------------------------------------------------------------- #

def test_tampered_cookie_rejected(env):
    owner = _client()
    _bootstrap_owner(owner)
    good = owner.cookies.get(team.COOKIE_NAME)
    raw, _, sig = good.rpartition(".")
    forged = f"{raw}.{'0' * len(sig)}"  # valid-looking but wrong signature
    attacker = _client()
    attacker.cookies.set(team.COOKIE_NAME, forged)
    assert attacker.get("/api/auth/me").status_code == 401
    # A token whose signature is stripped entirely is also rejected.
    attacker.cookies.set(team.COOKIE_NAME, raw)
    assert attacker.get("/api/auth/me").status_code == 401


def test_password_hashing_is_pbkdf2_constant_time(env):
    user_store.create_user("alice", "s3cret")
    rec = user_store.get_by_username("alice")
    assert rec is not None
    assert user_store.verify_password("alice", "s3cret")
    assert user_store.verify_password("alice", "wrong") is None
    # stored form is salted pbkdf2 — never plaintext
    import sqlite3
    conn = sqlite3.connect(config.db_path())
    h = conn.execute("SELECT password_hash FROM users WHERE username='alice'").fetchone()[0]
    conn.close()
    assert h.startswith("pbkdf2_sha256$")
    assert "s3cret" not in h
