"""Key extraction + optional gateway-key enforcement."""

import pytest

import config
import gateway.auth as auth
import storage.token_store as ts


@pytest.fixture(autouse=True)
def _no_tokens():
    """Isolate from the token store's module-level cache (set by other tests)."""
    ts._cache = {}
    yield


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_extract_x_api_key():
    assert auth.extract_key(_Req({"x-api-key": "k1"})) == "k1"


def test_extract_bearer():
    assert auth.extract_key(_Req({"authorization": "Bearer k2"})) == "k2"


def test_extract_goog_and_query():
    assert auth.extract_key(_Req({"x-goog-api-key": "k3"})) == "k3"
    assert auth.extract_key(_Req(query={"key": "k4"})) == "k4"


def test_check_key_open_by_default(monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_KEY", None)
    assert auth.check_key(_Req()) is True


def test_check_key_enforced(monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_KEY", "secret")
    assert auth.check_key(_Req({"x-api-key": "secret"})) is True
    assert auth.check_key(_Req({"x-api-key": "wrong"})) is False
    assert auth.check_key(_Req()) is False
