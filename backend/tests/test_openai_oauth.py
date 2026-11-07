"""ChatGPT subscription: OAuth (PKCE) manager, Codex credential derivation,
resolver routing and the sign-in routes. Hermetic: no network, no browser."""

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

import config
from gateway import openai_oauth as oa
from gateway import providers


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    oa.disconnect()
    return tmp_path


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _id_token(account="acc_1", org="org_1") -> str:
    auth = {"chatgpt_account_id": account}
    if org:
        auth["organization_id"] = org
    return f"{_b64url({'alg': 'none'})}.{_b64url({'https://api.openai.com/auth': auth})}.sig"


def test_pkce_verifier_and_challenge():
    v = oa._gen_verifier()
    assert len(v) == 43 and "=" not in v
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oa._challenge(v) == expected


def test_authorize_url_has_required_params():
    q = parse_qs(urlparse(oa.build_authorize_url("chal", "st")).query)
    assert q["client_id"] == [oa.CLIENT_ID]
    assert q["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == ["chal"] and q["state"] == ["st"]
    assert "offline_access" in q["scope"][0]


def test_decode_jwt_and_claims():
    claim = oa._auth_claim(_id_token("acc_9", "org_9"))
    assert claim == {"chatgpt_account_id": "acc_9", "organization_id": "org_9"}
    assert oa._auth_claim("garbage") == {}


def test_store_from_tokens_exchanges_api_key_when_org_present(home, monkeypatch):
    calls = []

    def fake_post(form):
        calls.append(form["grant_type"])
        if form["grant_type"] == oa.TOKEN_EXCHANGE_GRANT:
            assert form["requested_token"] == "openai-api-key"
            return {"access_token": "sk-exchanged"}
        return {}
    monkeypatch.setattr(oa, "_post_token", fake_post)
    blob = oa._store_from_tokens({"access_token": "raw-access", "id_token": _id_token("acc_1", "org_1"),
                                  "refresh_token": "refresh-1", "expires_in": 3600})
    assert blob["apiKey"] == "sk-exchanged" and blob["accountId"] == "acc_1"
    assert oa.TOKEN_EXCHANGE_GRANT in calls
    cat = {p["id"]: p for p in providers.catalog()}
    assert cat["openai"]["hasOauth"] is True and cat["openai"]["account"] == "acc_1"
    assert "sk-exchanged" not in repr(cat)


def test_store_from_tokens_skips_exchange_without_org(home, monkeypatch):
    monkeypatch.setattr(oa, "_post_token", lambda form: pytest.fail("must not exchange"))
    blob = oa._store_from_tokens({"access_token": "raw-access", "id_token": _id_token("acc_2", org=""),
                                  "refresh_token": "refresh-2"})
    assert blob["apiKey"] == "raw-access"


def test_refresh_uses_cache_then_refreshes(home, monkeypatch):
    providers.set_oauth("openai", {"refreshToken": "r0", "apiKey": "cached", "accountId": "acc",
                                   "expiresAt": 9_999_999_999})
    monkeypatch.setattr(oa, "_post_token", lambda form: pytest.fail("should not refresh"))
    assert oa._refresh() == ("cached", "acc")

    providers.set_oauth("openai", {"refreshToken": "r0", "apiKey": "old", "accountId": "acc", "expiresAt": 0})

    def fake_post(form):
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "r0"
        return {"access_token": "new-access", "id_token": _id_token("acc", org=""), "expires_in": 3600}
    monkeypatch.setattr(oa, "_post_token", fake_post)
    assert oa._refresh() == ("new-access", "acc")
    assert providers.get_oauth("openai")["refreshToken"] == "r0"


def test_refresh_failure_falls_back_to_cache(home, monkeypatch):
    providers.set_oauth("openai", {"refreshToken": "r0", "apiKey": "stale", "accountId": "acc", "expiresAt": 0})
    def boom(form):
        raise oa.OAuthError("down")
    monkeypatch.setattr(oa, "_post_token", boom)
    assert oa._refresh() == ("stale", "acc")


def test_refresh_without_login_returns_none(home):
    assert oa._refresh() == (None, None)


def test_login_status_reflects_connection(home):
    assert oa.login_status()["status"] == "idle"
    providers.set_oauth("openai", {"refreshToken": "r", "accountId": "acc_x"})
    s = oa.login_status()
    assert s["status"] == "connected" and s["account"] == "acc_x"
    oa.disconnect()
    assert oa.login_status()["status"] == "idle"


def test_resolve_target_openai_subscription_is_codex(home, monkeypatch):
    async def fake_cred():
        return "sk-live", "acc_42"
    monkeypatch.setattr(oa, "active_credential", fake_cred)
    model, target = asyncio.run(providers.resolve_target(
        {"modelSource": "subscription", "provider": "openai", "model": "gpt-4o"}))
    assert model == "gpt-4o"
    assert target["kind"] == "codex" and target["base_url"] == oa.CODEX_URL
    assert target["api_key"] == "sk-live" and target["account_id"] == "acc_42"
    assert target["auth_mode"] == "subscription"


def test_resolve_target_openai_subscription_not_signed_in(home, monkeypatch):
    async def none():
        return None, None
    monkeypatch.setattr(oa, "active_credential", none)
    with pytest.raises(ValueError, match="not signed in"):
        asyncio.run(providers.resolve_target({"modelSource": "subscription", "provider": "openai"}))


def test_resolve_target_openai_subscription_default_model(home, monkeypatch):
    async def fake_cred():
        return "sk-live", "acc_42"
    monkeypatch.setattr(oa, "active_credential", fake_cred)
    model, _ = asyncio.run(providers.resolve_target({"modelSource": "subscription", "provider": "openai"}))
    assert model == providers.PROVIDER_DEFS["openai"]["subscriptionModels"][0]


def test_oauth_routes(home, monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setattr(oa, "_ensure_server", lambda: None)  # no real listener in tests
    with TestClient(app) as c:
        assert c.get("/api/providers/openai/oauth/status").json()["status"] == "idle"
        r = c.post("/api/providers/openai/oauth/start")
        assert r.status_code == 200 and r.json()["authUrl"].startswith(oa.AUTHORIZE_URL)
        assert c.get("/api/providers/openai/oauth/status").json()["status"] == "awaiting"
        providers.set_oauth("openai", {"refreshToken": "r", "accountId": "acc_x"})
        assert c.get("/api/providers/openai/oauth/status").json()["status"] == "connected"
        assert c.post("/api/providers/openai/oauth/disconnect").json()["status"] == "idle"
        assert providers.get_oauth("openai") == {}

    def fail():
        raise oa.OAuthError("port busy")
    monkeypatch.setattr(oa, "_ensure_server", fail)
    with TestClient(app) as c:
        assert c.post("/api/providers/openai/oauth/start").status_code == 409
