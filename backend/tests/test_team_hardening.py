"""Team-mode hardening — role floor on mutating routes, audit log, login rate
limit, secure cookie behind TLS. Solo mode stays untouched."""

import pytest
from fastapi.testclient import TestClient

import config
import team
import storage.token_store as ts
from app import app
from middleware.team_auth import required_role
from storage import audit_store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    ts._cache = None
    ts._last_used_flush.clear()
    team.login_limiter._fails.clear()
    yield tmp_path


def _client():
    return TestClient(app)


def _bootstrap(c):
    r = c.post("/api/auth/register", json={"username": "owner", "password": "pw-owner-1"})
    assert r.status_code == 200 and r.json()["teamMode"] is True
    return c


def _invite_and_login(owner, username, role):
    r = owner.post("/api/auth/register", json={"username": username, "password": f"pw-{username}-1", "role": role})
    assert r.status_code == 200, r.text
    c = _client()
    r = c.post("/api/auth/login", json={"username": username, "password": f"pw-{username}-1"})
    assert r.status_code == 200, r.text
    return c


def test_required_role_policy():
    assert required_role("GET", "/api/providers") is None
    assert required_role("POST", "/api/providers/openai") == "admin"
    assert required_role("PUT", "/api/providers") == "admin"
    assert required_role("POST", "/api/channels/accounts") == "admin"
    assert required_role("POST", "/api/agents") == "member"
    assert required_role("POST", "/api/agents/x/tasks") == "member"
    assert required_role("DELETE", "/api/debug/sessions") == "admin"
    assert required_role("POST", "/api/auth/disable-team") == "owner"
    assert required_role("POST", "/api/auth/logout") == "viewer"
    assert required_role("POST", "/api/something-new") == "member"      # safe default
    assert required_role("POST", "/v1/chat/completions") is None


def test_roles_are_enforced_and_audited(env):
    owner = _bootstrap(_client())
    member = _invite_and_login(owner, "mia", "member")
    viewer = _invite_and_login(owner, "vic", "viewer")

    # viewer: reads yes, writes no
    assert viewer.get("/api/agents").status_code == 200
    assert viewer.post("/api/agents", json={"name": "x"}).status_code == 403
    # member: operate workers yes, installation config no
    r = member.post("/api/agents", json={"name": "Worker A", "persona": "p"})
    assert r.status_code == 200, r.text
    assert member.put("/api/providers/openai", json={"apiKey": "sk-x"}).status_code == 403
    assert member.post("/api/channels/accounts", json={"kind": "telegram", "name": "t"}).status_code == 403
    assert member.get("/api/team/audit").status_code == 403
    # owner: everything
    r = owner.post("/api/channels/accounts", json={"kind": "telegram", "name": "t", "enabled": False})
    assert r.status_code == 200, r.text
    # audit log: the writes above are recorded with their principal and outcome
    entries = owner.get("/api/team/audit").json()["entries"]
    by = {(e["username"], e["method"], e["path"].split("?")[0], e["status"]) for e in entries}
    assert ("vic", "POST", "/api/agents", 403) in by
    assert ("mia", "POST", "/api/agents", 200) in by
    assert ("mia", "PUT", "/api/providers/openai", 403) in by
    assert ("owner", "POST", "/api/channels/accounts", 200) in by
    assert all(e["role"] for e in entries if e["userId"])   # anonymous rows only for login/bootstrap
    # a viewer's logout is allowed
    assert viewer.post("/api/auth/logout").status_code == 200


def test_unauthenticated_and_webhooks(env):
    _bootstrap(_client())
    anon = _client()
    assert anon.get("/api/agents").status_code == 401
    assert anon.post("/api/agents", json={"name": "x"}).status_code == 401
    assert anon.post("/api/triggers/hook/nope", json={}).status_code == 404      # own guard, not 401
    assert anon.get("/api/channels/webhook/whatsapp_cloud/nope").status_code == 404
    # webhook calls are not audited (machine traffic with its own proof)
    assert all(not e["path"].startswith("/api/triggers/hook") for e in audit_store.list_recent())


def test_login_rate_limit(env):
    _bootstrap(_client())
    c = _client()
    for _ in range(10):
        assert c.post("/api/auth/login", json={"username": "owner", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "owner", "password": "wrong"}).status_code == 429
    # even the right password is refused while blocked
    assert c.post("/api/auth/login", json={"username": "owner", "password": "pw-owner-1"}).status_code == 429
    team.login_limiter._fails.clear()
    assert c.post("/api/auth/login", json={"username": "owner", "password": "pw-owner-1"}).status_code == 200


def test_cookie_flags(env):
    c = _client()
    r = c.post("/api/auth/register", json={"username": "owner", "password": "pw-owner-1"})
    sc = r.headers.get("set-cookie", "")
    assert "vw_session=" in sc and "HttpOnly" in sc and "SameSite=strict" in sc and "Secure" not in sc
    c2 = _client()
    r = c2.post("/api/auth/login", json={"username": "owner", "password": "pw-owner-1"},
                headers={"x-forwarded-proto": "https"})
    assert "Secure" in r.headers.get("set-cookie", "")


def test_solo_mode_unchanged(env):
    c = _client()
    assert c.get("/api/agents").status_code == 200
    assert c.post("/api/agents", json={"name": "solo", "persona": "p"}).status_code == 200
    assert audit_store.list_recent() == []        # nothing to attribute in solo mode
    assert c.get("/api/team/audit").status_code == 200   # readable, empty
