"""WhatsApp adapters — pure-function parsing + the webhook route (no network)."""

import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import config
from channels.whatsapp import cloud, linked
from storage import chat_channel_store as store


def test_linked_jid_helpers():
    assert linked.jid_user("6281234@s.whatsapp.net") == "6281234"
    assert linked.jid_user("6281234:12@lid") == "6281234"
    assert linked.jid_user(None) == ""
    assert linked.to_jid("+62 812 34") == "6281234@s.whatsapp.net"
    assert linked.to_jid("1203630-1600000@g.us") == "1203630-1600000@g.us"


def test_linked_parse_bridge_message_dm_and_group():
    dm = linked.parse_bridge_message("acct", {
        "id": "ABC", "chat": "628111@s.whatsapp.net", "group": False, "sender": "628111@s.whatsapp.net",
        "senderName": "Bob", "text": "hi", "ts": "2026-11-04T00:00:00Z", "mentioned": False,
        "fromMe": False, "replyTo": None, "media": None, "key": {"id": "ABC"}})
    assert dm.kind == "dm" and dm.sender_id == "628111" and dm.conversation_id == "628111@s.whatsapp.net"
    assert dm.event_id == "ABC" and dm.session_key == "whatsapp:acct:628111@s.whatsapp.net"
    grp = linked.parse_bridge_message("acct", {
        "id": "G1", "chat": "123-456@g.us", "group": True, "sender": "628222:5@lid",
        "senderName": "Ann", "text": "@bot help", "mentioned": True, "fromMe": False,
        "media": {"path": "/m/a.pdf", "mime": "application/pdf", "name": "a.pdf", "kind": "document", "caption": ""}})
    assert grp.kind == "group" and grp.sender_id == "628222" and grp.mentioned
    assert grp.attachments[0].kind == "document" and grp.attachments[0].path == "/m/a.pdf"


def test_cloud_signature_and_payload_parsing():
    body = b'{"x":1}'
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert cloud.verify_signature("sec", body, sig)
    assert not cloud.verify_signature("sec", body, "sha256=deadbeef")
    assert not cloud.verify_signature("sec", body, None)
    assert not cloud.verify_signature("", body, sig)
    payload = {"entry": [{"changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PN"},
                                        "contacts": [{"wa_id": "628111", "profile": {"name": "Bob"}}],
                                        "messages": [{"from": "628111", "id": "wamid.1", "timestamp": "1",
                                                      "type": "text", "text": {"body": "hello"}}]}},
        {"field": "messages", "value": {"statuses": [{"id": "wamid.0", "status": "read"}]}},
    ]}]}
    items = cloud.parse_webhook("acct", payload)
    assert len(items) == 1 and items[0]["contactName"] == "Bob" and items[0]["phoneNumberId"] == "PN"


def test_cloud_webhook_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import channels.whatsapp  # noqa: F401
    from app import app
    from channels.manager import manager

    acct = store.create_account("whatsapp_cloud", "biz", enabled=True, config={"phoneNumberId": "PN", "dmPolicy": "open"})
    store.set_secrets(acct["id"], {"accessToken": "t", "appSecret": "sec", "verifyToken": "vt"})
    received = []

    async def capture(msg):
        received.append(msg)

    with TestClient(app) as c:   # startup hook starts the enabled account
        adapter = manager.adapter(acct["id"])
        assert adapter is not None and adapter.status()["state"] == "open"
        adapter.on_inbound = capture
        url = f"/api/channels/webhook/whatsapp_cloud/{acct['id']}"
        r = c.get(url, params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "12345"})
        assert r.status_code == 200 and r.text == "12345"
        assert c.get(url, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403
        body = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN"}, "contacts": [{"wa_id": "628111", "profile": {"name": "Bob"}}],
            "messages": [{"from": "628111", "id": "wamid.9", "timestamp": "1", "type": "text", "text": {"body": "yo"}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        assert c.post(url, content=body, headers={"x-hub-signature-256": "sha256=bad", "content-type": "application/json"}).status_code == 403
        r = c.post(url, content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
        assert r.status_code == 200 and r.json()["messages"] == 1
        assert c.get("/api/channels/webhook/whatsapp_cloud/nope").status_code == 404
    assert received and received[0].text == "yo" and received[0].sender_name == "Bob"
    assert received[0].conversation_id == "628111" and received[0].event_id == "wamid.9"


def test_channels_api_masks_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import channels.whatsapp  # noqa: F401
    from app import app
    with TestClient(app) as c:
        r = c.post("/api/channels/accounts", json={"kind": "whatsapp_cloud", "name": "biz",
                                                   "config": {"phoneNumberId": "PN"},
                                                   "secrets": {"accessToken": "TOKEN-123"}})
        assert r.status_code == 200
        acct = r.json()
        assert acct["secrets"] == {"hasAccessToken": True, "hasAppSecret": False, "hasVerifyToken": False}
        assert "TOKEN-123" not in r.text
        listing = c.get("/api/channels").json()
        assert {"whatsapp", "whatsapp_cloud"} <= {k["kind"] for k in listing["kinds"]}
        assert "TOKEN-123" not in json.dumps(listing)
        assert c.post("/api/channels/accounts", json={"kind": "nope"}).status_code == 400
        r = c.post(f"/api/channels/accounts/{acct['id']}/bindings", json={"conversationId": "628111", "agentId": "missing"})
        assert r.status_code == 400
        assert c.delete(f"/api/channels/accounts/{acct['id']}").json()["ok"] is True


def test_send_converts_markdown_to_whatsapp_markup(tmp_path, monkeypatch):
    import asyncio as _asyncio

    import httpx as _httpx

    from channels.whatsapp import cloud as _cloud, linked as _linked

    sent: list[dict] = []

    async def fake_command(cmd, wait=True):
        sent.append(cmd)
        return "OUT"

    la = _linked.WhatsAppLinkedAdapter({"id": "a", "kind": "whatsapp", "config": {}}, {},
                                       on_inbound=lambda m: _asyncio.sleep(0), data_dir=str(tmp_path))
    la._status["state"] = "open"
    monkeypatch.setattr(la, "_command", fake_command)
    _asyncio.run(la.send("628111", "**Halo** Kak"))
    assert sent[-1]["text"] == "*Halo* Kak"

    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    real_client = _httpx.AsyncClient
    monkeypatch.setattr(_cloud.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_httpx.MockTransport(handler)))
    ca = _cloud.WhatsAppCloudAdapter({"id": "c", "kind": "whatsapp_cloud",
                                      "config": {"phoneNumberId": "1"}}, {"accessToken": "t"},
                                     on_inbound=lambda m: _asyncio.sleep(0), data_dir=str(tmp_path))
    assert _asyncio.run(ca.send("628111", "## Info\n**KTP**")) == "wamid.1"
    assert bodies[-1]["text"]["body"] == "*Info*\n*KTP*"
