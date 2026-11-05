"""Email delivery — SMTP connector behind an allow-list, secrets write-only."""

import asyncio

from fastapi.testclient import TestClient

import config
from agent import delivery
from storage import channel_store


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, use_ssl):
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.started_tls = False
        self.login_args = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def _setup(tmp_path, monkeypatch, allow):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    FakeSMTP.instances.clear()
    monkeypatch.setattr(delivery, "_smtp_factory", lambda h, p, ssl: FakeSMTP(h, p, ssl))
    t = channel_store.create_channel("email", "work mail", enabled=True, config={
        "host": "smtp.example.com", "port": 587, "username": "bot@example.com",
        "from": "bot@example.com", "allowTo": allow})
    channel_store.set_secrets(t["id"], {"password": "hunter2"})
    return t


def test_email_send_respects_allowlist_and_uses_secret(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, ["boss@example.com", "@team.example"])
    rec = asyncio.run(delivery.deliver("email", "boss@example.com", "report body", subject="Daily"))
    assert rec["status"] == "sent", rec
    smtp = FakeSMTP.instances[-1]
    assert smtp.host == "smtp.example.com" and smtp.started_tls and smtp.login_args == ("bot@example.com", "hunter2")
    m = smtp.sent[0]
    assert m["To"] == "boss@example.com" and m["Subject"] == "Daily" and "report body" in m.get_content()
    rec = asyncio.run(delivery.deliver("email", "anyone@team.example", "x"))
    assert rec["status"] == "sent"
    rec = asyncio.run(delivery.deliver("email", "stranger@elsewhere.com", "x"))
    assert rec["status"] == "failed" and "allow-list" in rec["error"]
    rec = asyncio.run(delivery.deliver("email", "", "x"))
    assert rec["status"] == "failed"


def test_disabled_target_refuses(tmp_path, monkeypatch):
    t = _setup(tmp_path, monkeypatch, ["*"])
    channel_store.update_channel(t["id"], enabled=False)
    rec = asyncio.run(delivery.deliver("email", "x@y.z", "hi"))
    assert rec["status"] == "failed"


def test_ssl_port_uses_ssl(tmp_path, monkeypatch):
    t = _setup(tmp_path, monkeypatch, ["*"])
    channel_store.update_channel(t["id"], config={**channel_store.get_channel(t["id"])["config"], "port": 465})
    rec = asyncio.run(delivery.deliver("email", "x@y.z", "hi"))
    assert rec["status"] == "sent" and FakeSMTP.instances[-1].use_ssl and not FakeSMTP.instances[-1].started_tls


def test_delivery_target_api_masks_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from app import app
    with TestClient(app) as c:
        r = c.post("/api/delivery-targets", json={"kind": "email", "name": "mail", "enabled": True,
                                                  "config": {"host": "smtp.x", "port": 587, "username": "u", "from": "u@x", "allowTo": ["a@b.c"]},
                                                  "secrets": {"password": "PW-SECRET"}})
        assert r.status_code == 200 and r.json()["secrets"] == {"hasPassword": True}
        assert "PW-SECRET" not in r.text
        cid = r.json()["id"]
        listing = c.get("/api/delivery-targets").text
        assert "PW-SECRET" not in listing and "hasPassword" in listing
        r = c.patch(f"/api/delivery-targets/{cid}", json={"kind": "email", "secrets": {"password": ""}})
        assert r.json()["secrets"] == {}
        assert c.delete(f"/api/delivery-targets/{cid}").json()["ok"]
        assert channel_store.get_secrets(cid) == {}
