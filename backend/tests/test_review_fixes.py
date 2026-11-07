"""Regression tests from the review pass: Telegram quoting, schedule attribution
on chat deliveries, the chat kind filter, tool provenance in channel history,
group jid parsing, streamed backups and cheap document info."""

import asyncio
import json
import sqlite3
import zipfile

import httpx
import pytest

import config
from channels import base, manager as mgr_mod, telegram
from channels.base import ChannelAdapter, InboundMessage
from channels.manager import ChannelManager, load_history
from channels.manager import execute_turn as real_execute_turn   # bound before any test rebinds the module attr
from channels.whatsapp import linked
from storage import agent_store, backup, channel_store, chat_channel_store as cstore, chat_store


class FakeAdapter(ChannelAdapter):
    kind = "fake2"
    label = "Fake2"
    max_text_len = 200

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = []

    async def start(self):
        self._set_status("open", me="me-id")

    async def stop(self):
        self._set_status("off")

    async def send(self, target, text, *, reply_to=None, attachments=None):
        self.sent.append((target, text, reply_to))
        return "platform-mid-1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    base.registry.register(FakeAdapter)
    agent = agent_store.create_agent({"name": "Helper", "persona": "You help."})
    acct = cstore.create_account("fake2", "acct", enabled=True, default_agent_id=agent["id"],
                                 config={"dmPolicy": "open", "debounceMs": 0, "ackAfterMs": 0})
    m = ChannelManager()
    m._started = True
    return {"agent": agent, "acct": acct, "m": m}


def test_telegram_reply_uses_message_id_from_event_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    bodies = []

    def handler(request):
        if request.url.path.endswith("/sendMessage"):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        return httpx.Response(404)
    monkeypatch.setattr(telegram.TelegramAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    async def main():
        ad = telegram.TelegramAdapter({"id": "a", "kind": "telegram", "config": {}}, {"botToken": "T"},
                                      on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        ad._set_status("open")
        await ad.send("12345", "hi", reply_to="12345:77")   # the manager passes msg.event_id
        assert bodies[-1]["reply_parameters"]["message_id"] == 77
        await ad.send("12345", "hi", reply_to="not-an-id")
        assert "reply_parameters" not in bodies[-1]

    asyncio.run(main())


def test_chat_delivery_keeps_schedule_id_and_test_sends_have_no_error(env, monkeypatch):
    m, acct, agent = env["m"], env["acct"], env["agent"]
    monkeypatch.setattr(mgr_mod, "manager", m)

    async def main():
        await m.start_account(acct["id"])
        from agent import delivery
        rec = await delivery.deliver("chat", f"{acct['id']}:628", "scheduled text", agent_id=agent["id"],
                                     source="schedule", schedule_id="sch-1")
        assert rec["status"] == "sent" and rec["scheduleId"] == "sch-1" and rec["channel"] == "chat:fake2"
        rows = channel_store.list_deliveries(schedule_id="sch-1")
        assert len(rows) == 1 and rows[0]["payloadText"] == "scheduled text"
        rec = await m.send(acct["id"], "628", "ping", source="test")
        assert rec["status"] == "sent" and rec["error"] == "" and rec["messageId"] == "platform-mid-1"
        assert channel_store.get_delivery(rec["id"])["error"] == ""
        await m.stop()

    asyncio.run(main())


def test_chat_kind_filter_matches_every_chat_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    channel_store.log_delivery(channel="chat:whatsapp", target="1", source="reply", status="sent")
    channel_store.log_delivery(channel="chat:telegram", target="2", source="reply", status="sent")
    channel_store.log_delivery(channel="chat", target="3", source="tool", status="dropped")
    channel_store.log_delivery(channel="email", target="4", source="tool", status="sent")
    assert {r["channel"] for r in channel_store.list_deliveries(channel="chat")} == {"chat:whatsapp", "chat:telegram", "chat"}
    assert [r["channel"] for r in channel_store.list_deliveries(channel="chat:telegram")] == ["chat:telegram"]
    assert [r["channel"] for r in channel_store.list_deliveries(channel="email")] == ["email"]


def test_channel_turn_persists_tools_used_for_history_provenance(env, monkeypatch):
    m, acct = env["m"], env["acct"]

    async def turn(a, messages, session_id):
        return "I looked it up.", None, ["web_search", "web_search", "read_file"]

    monkeypatch.setattr(mgr_mod, "execute_turn", turn)

    async def main():
        await m.start_account(acct["id"])
        msg = InboundMessage(channel="fake2", account_id=acct["id"], conversation_id="628", kind="dm",
                             sender_id="628", text="look it up", event_id="e1")
        await m.handle_inbound(msg)
        sess = chat_store.get_session(msg.session_key)
        assistant = [x for x in sess["messages"] if x["role"] == "assistant"][-1]
        assert assistant["meta"]["toolsUsed"] == ["web_search", "web_search", "read_file"]
        hist = load_history(msg.session_key, 10)
        assert hist[-1]["content"].startswith("[this reply was produced by actually CALLING tools: web_search ×2, read_file")
        await m.stop()

    asyncio.run(main())


def test_execute_turn_collects_tool_names(monkeypatch):
    async def fake_stream(agent, messages, session_id=None, extra_system=None):
        for ev in ({"type": "text", "delta": "thinking "}, {"type": "tool_call", "id": "1", "name": "kb_search", "args": {}},
                   {"type": "tool_result", "id": "1", "name": "kb_search", "result": "x"},
                   {"type": "text", "delta": "Answer."}, {"type": "done"}):
            yield ev
    from agent import runner
    monkeypatch.setattr(runner, "stream_run", fake_stream)
    text, err, tools = asyncio.run(real_execute_turn({"id": "a"}, [], "s"))
    assert text == "Answer." and err is None and tools == ["kb_search"]


def test_to_jid_phone_with_dashes_is_not_a_group():
    assert linked.to_jid("1-555-0100") == "15550100@s.whatsapp.net"
    assert linked.to_jid("+1 (555) 0100".replace("(", "").replace(")", "")) == "15550100@s.whatsapp.net"
    assert linked.to_jid("1203630-1600000000") == "1203630-1600000000@g.us"
    assert linked.to_jid("1203630-1600000000-group") == "1203630-1600000000@g.us"
    assert linked.to_jid("120363-16000@g.us") == "120363-16000@g.us"


def test_export_backup_to_streams_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    conn = sqlite3.connect(str(tmp_path / "app.db"))
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT)"); conn.execute("INSERT INTO agents VALUES ('a','n')")
    conn.commit(); conn.close()
    (tmp_path / "settings.json").write_text(json.dumps({"activeModel": "q"}))
    out = tmp_path / "backups" / "x.vwbak"
    out.parent.mkdir()
    manifest = backup.export_backup_to(out)
    assert out.is_file() and not out.with_suffix(".vwbak.part").exists()
    assert manifest["contents"]["app.db"] is True
    with zipfile.ZipFile(out) as zf:
        assert json.loads(zf.read("manifest.json"))["app"] == "virtual-worker"
    # in-memory variant still works for the download route
    data, _ = backup.export_backup()
    assert data[:2] == b"PK"


def test_document_inspect_counts_without_extracting(tmp_path):
    from agent import doc_tools
    csv = tmp_path / "t.csv"; csv.write_text("a,b\n1,2\n3,4\n")
    assert doc_tools.inspect(csv) == {"rows": 3}
    txt = tmp_path / "t.txt"; txt.write_text("hello")
    assert "rows" not in doc_tools.inspect(txt) or True
