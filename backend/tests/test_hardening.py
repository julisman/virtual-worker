"""Hardening — CORS is local-only, the Host header is checked, webhooks pass the
team-mode auth gate, and logs scrub credential-shaped strings."""

import logging

from fastapi.testclient import TestClient

import config
import team
from app import app
from storage import user_store


def _client():
    return TestClient(app)


def test_cors_allows_local_origins_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    c = _client()
    ok = c.options("/api/agents", headers={"Origin": "http://localhost:5173",
                                          "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    ok2 = c.get("/health", headers={"Origin": "http://127.0.0.1:8888"})
    assert ok2.headers.get("access-control-allow-origin") == "http://127.0.0.1:8888"
    bad = c.options("/api/agents", headers={"Origin": "https://evil.example",
                                           "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
    bad2 = c.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad2.headers


def test_untrusted_host_header_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    c = _client()
    assert c.get("/health", headers={"Host": "localhost:8888"}).status_code == 200
    r = c.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_webhook_paths_pass_team_auth_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(team, "is_enabled", lambda: True)
    monkeypatch.setattr(user_store, "resolve_session", lambda raw: None)
    c = _client()
    # ordinary API is gated…
    assert c.get("/api/agents").status_code == 401
    # …webhooks reach their own guards (404 = no such webhook, not 401)
    assert c.get("/api/channels/webhook/whatsapp_cloud/nope").status_code == 404
    assert c.post("/api/triggers/hook/nope", json={}).status_code == 404


def test_log_filter_scrubs_tokens(caplog):
    from app import _RedactFilter
    rec = logging.LogRecord("x", logging.INFO, "f", 1,
                            "GET https://api.telegram.org/bot123456789:AAHxyz_abc-DEF0123456789abc/getUpdates "
                            "Authorization: Bearer abcdefghijklmnop sk-vw-abcdefghijklmnopqrstuvwxyz", (), None)
    assert _RedactFilter().filter(rec) is True
    msg = rec.getMessage()
    assert "AAHxyz" not in msg and "bot<redacted>" in msg
    assert "abcdefghijklmnop" not in msg.split("Bearer")[1].split()[0]
    assert "sk-<redacted>" in msg
