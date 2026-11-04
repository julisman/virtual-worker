"""WhatsAppLinkedAdapter ↔ bridge protocol over a real child process.

A tiny Python script stands in for the Node bridge (same JSON-lines protocol),
so this exercises spawning, status events, inbound messages, the send
round-trip, pairing payloads, and restart-with-backoff — without Node or
WhatsApp."""

import asyncio
import sys
import textwrap

import pytest

import config
from channels.whatsapp import linked

FAKE_BRIDGE = textwrap.dedent(r'''
    import json, sys, os
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    emit({"type": "status", "state": "connecting"})
    emit({"type": "status", "state": "qr", "qr": "data:image/png;base64,QR"})
    emit({"type": "status", "state": "open", "me": "628000:1@s.whatsapp.net", "name": "Bot"})
    emit({"type": "message", "id": "M1", "chat": "628111@s.whatsapp.net", "group": False,
          "sender": "628111@s.whatsapp.net", "senderName": "Bob", "text": "hello", "fromMe": False,
          "media": None, "key": {"id": "M1"}})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["type"] == "send":
            emit({"type": "result", "id": cmd["id"], "ok": True,
                  "messageId": "OUT-" + cmd["to"] + ("+quoted" if cmd.get("quotedKey") else "")})
        elif cmd["type"] == "pair":
            emit({"type": "status", "state": "qr", "pairingCode": "ABCD-1234"})
            emit({"type": "result", "id": cmd["id"], "ok": True, "messageId": "ABCD-1234"})
        elif cmd["type"] == "crash":
            os._exit(3)
        elif cmd["type"] == "logout":
            emit({"type": "result", "id": cmd["id"], "ok": True}); break
        else:
            emit({"type": "result", "id": cmd.get("id"), "ok": False, "error": "unknown"})
''')


@pytest.fixture
def fake_bridge(tmp_path, monkeypatch):
    script = tmp_path / "fake_bridge.py"
    script.write_text(FAKE_BRIDGE)
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(linked, "BRIDGE_MAIN", script)
    monkeypatch.setattr(linked, "node_bin", lambda: sys.executable)
    monkeypatch.setattr(linked, "bridge_installed", lambda: True)
    return script


async def _wait_state(adapter, state, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if adapter.status().get("state") == state:
            return True
        await asyncio.sleep(0.05)
    return False


def test_linked_adapter_full_round_trip(fake_bridge, tmp_path):
    async def main():
        inbound = []

        async def on_inbound(m):
            inbound.append(m)

        acct = {"id": "acct1", "kind": "whatsapp", "config": {"phone": "628000"}}
        ad = linked.WhatsAppLinkedAdapter(acct, {}, on_inbound=on_inbound, data_dir=str(tmp_path / "d"))
        await ad.start()
        assert await _wait_state(ad, "open")
        st = ad.status()
        assert st["me"] == "628000" and st["name"] == "Bot" and st["hasQr"] is False
        assert ad.self_ids() == {"628000", "628000@s.whatsapp.net"}
        # inbound message was normalized and delivered
        await asyncio.sleep(0.1)
        assert inbound and inbound[0].sender_id == "628111" and inbound[0].text == "hello"
        assert inbound[0].session_key == "whatsapp:acct1:628111@s.whatsapp.net"
        # send round-trip through stdin/stdout
        mid = await ad.send("628111", "hi there")
        assert mid == "OUT-628111@s.whatsapp.net"
        mid = await ad.send("628111", "quoted reply", reply_to="M1")
        assert mid == "OUT-628111@s.whatsapp.net+quoted"
        # pairing code request
        ad._status["state"] = "qr"
        out = await ad.pair()
        assert out["pairingCode"] == "ABCD-1234"
        # crash → the adapter notices and restarts the bridge
        await ad._command({"type": "crash"}, wait=False)
        assert await _wait_state(ad, "reconnecting", timeout=5)
        assert await _wait_state(ad, "open", timeout=10)
        await ad.stop()
        assert ad.status()["state"] == "off"

    asyncio.run(main())


def test_linked_adapter_refuses_send_when_not_open(fake_bridge, tmp_path):
    async def main():
        ad = linked.WhatsAppLinkedAdapter({"id": "a", "kind": "whatsapp", "config": {}}, {},
                                          on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path / "d"))
        with pytest.raises(RuntimeError):
            await ad.send("628111", "x")
        await ad.stop()

    asyncio.run(main())


def test_linked_adapter_reports_missing_node(tmp_path, monkeypatch):
    monkeypatch.setattr(linked, "node_bin", lambda: None)

    async def main():
        ad = linked.WhatsAppLinkedAdapter({"id": "a", "kind": "whatsapp", "config": {}}, {},
                                          on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        await ad.start()
        assert ad.status()["state"] == "error" and "Node" in ad.status()["error"]

    asyncio.run(main())
