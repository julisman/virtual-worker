"""API token store + gateway auth enforcement."""

import pytest

import config
import gateway.auth as auth
import storage.token_store as ts


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(config, "GATEWAY_KEY", None)
    ts._cache = None
    ts._last_used_flush.clear()
    return ts


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_create_returns_plaintext_once(store):
    rec = store.create("prod")
    assert rec["token"].startswith("sk-vw-")
    assert rec["prefix"].startswith("sk-vw-")
    # list never exposes plaintext
    listed = store.list_tokens()
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["name"] == "prod"
    assert listed[0]["revoked"] is False


def test_verify_and_revoke(store):
    rec = store.create("t")
    assert store.has_active() is True
    assert store.verify(rec["token"]) is True
    assert store.verify("sk-vw-wrong") is False
    assert store.revoke(rec["id"]) is True
    assert store.verify(rec["token"]) is False
    assert store.has_active() is False


def test_expired_token_invalid(store):
    rec = store.create("short", ttl_days=1)
    # force it into the past via the cache record
    store._load_cache()
    for r in store._cache.values():
        r["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert store.verify(rec["token"]) is False
    assert store.has_active() is False


def test_auth_open_until_first_token(store):
    # no tokens, no env key -> open
    assert auth.gateway_key_required() is False
    assert auth.check_key(_Req()) is True
    # create one -> now required
    rec = store.create("k")
    assert auth.gateway_key_required() is True
    assert auth.check_key(_Req()) is False
    assert auth.check_key(_Req({"x-api-key": rec["token"]})) is True
    assert auth.check_key(_Req({"authorization": f"Bearer {rec['token']}"})) is True
    assert auth.check_key(_Req({"x-api-key": "nope"})) is False


def test_static_env_key_still_works(store, monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_KEY", "secret")
    assert auth.check_key(_Req({"x-api-key": "secret"})) is True
    assert auth.check_key(_Req({"x-api-key": "nope"})) is False
