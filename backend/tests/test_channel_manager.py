"""ChannelManager — policies, dedup, FIFO, commands, reply handling (no model,
no network): a fake adapter records sends; execute_turn is monkeypatched."""

import asyncio

import pytest

import config
from channels import base, manager as mgr_mod
from channels.base import Attachment, ChannelAdapter, InboundMessage, split_text
from channels.manager import ChannelManager, strip_reasoning
from storage import agent_store, channel_store, chat_channel_store as store, chat_store


class FakeAdapter(ChannelAdapter):
    kind = "fake"
    label = "Fake"
    capabilities = {**ChannelAdapter.capabilities, "typing": True}
    max_text_len = 200

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, str, str | None]] = []
        self.typing_calls: list[bool] = []

    async def start(self):
        self._set_status("open", me="me-id")

    async def stop(self):
        self._set_status("off")

    async def send(self, target, text, *, reply_to=None, attachments=None):
        self.sent.append((target, text, reply_to))
        return f"mid-{len(self.sent)}"

    async def typing(self, target, on=True):
        self.typing_calls.append(on)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    base.registry.register(FakeAdapter)
    agent = agent_store.create_agent({"name": "Helper", "persona": "You help."})
    acct = store.create_account("fake", "fake acct", enabled=True, default_agent_id=agent["id"],
                                config={"dmPolicy": "open", "debounceMs": 0, "ackAfterMs": 0})
    m = ChannelManager()
    m._started = True
    return {"agent": agent, "acct": acct, "m": m, "tmp": tmp_path}


def _msg(acct, text, *, sender="628111", conv=None, kind="dm", event=None, mentioned=False, name="Bob"):
    return InboundMessage(channel="fake", account_id=acct["id"], conversation_id=conv or sender,
                          kind=kind, sender_id=sender, sender_name=name, text=text,
                          event_id=event or f"e-{text}", mentioned=mentioned)


async def _run(m, acct, *msgs, turn=None):
    await m.start_account(acct["id"])
    if turn is not None:
        mgr_mod.execute_turn = turn
    await asyncio.gather(*(m.handle_inbound(x) for x in msgs))
    return m.adapter(acct["id"])


def test_split_and_strip():
    assert split_text("", 10) == []
    chunks = split_text("para one\n\npara two is longer\n\nthree", 20)
    assert chunks == ["para one", "para two is longer", "three"]
    assert all(len(c) <= 20 for c in split_text("x" * 95, 20))
    assert strip_reasoning("<think>secret</think>Hello\n\n_(stopped: reached max tool iterations)_") == "Hello"


def test_open_dm_runs_agent_persists_session_and_audits(env):
    m, acct, agent = env["m"], env["acct"], env["agent"]
    seen = {}

    async def fake_turn(a, messages, session_id):
        seen["messages"] = messages
        seen["session"] = session_id
        return "<think>hmm</think>Hi Bob! " + "x" * 210, None

    ad = asyncio.run(_run(m, acct, _msg(acct, "hello"), turn=fake_turn))
    assert seen["session"] == f"fake:{acct['id']}:628111"
    assert seen["messages"][-1] == {"role": "user", "content": "hello"}
    assert len(ad.sent) == 2 and ad.sent[0][1].startswith("Hi Bob!") and ad.sent[0][2] == "e-hello"
    assert ad.sent[1][2] is None
    assert ad.typing_calls and ad.typing_calls[-1] is False
    sess = chat_store.get_session(seen["session"])
    assert [x["role"] for x in sess["messages"]] == ["user", "assistant"]
    dels = channel_store.list_deliveries()
    assert dels and dels[0]["status"] == "sent" and dels[0]["channel"] == "chat:fake"
    assert dels[0]["agentId"] == agent["id"]


def test_history_is_replayed_on_follow_up(env):
    m, acct = env["m"], env["acct"]
    turns = []

    async def fake_turn(a, messages, session_id):
        turns.append(list(messages))
        return f"reply {len(turns)}", None

    asyncio.run(_run(m, acct, _msg(acct, "first"), turn=fake_turn))
    asyncio.run(_run(m, acct, _msg(acct, "second"), turn=fake_turn))
    assert [x["content"] for x in turns[1]] == ["first", "reply 1", "second"]


def test_dedup_drops_redelivered_event(env):
    m, acct = env["m"], env["acct"]
    n = {"runs": 0}

    async def fake_turn(a, messages, session_id):
        n["runs"] += 1
        return "ok", None

    asyncio.run(_run(m, acct, _msg(acct, "a", event="same"), _msg(acct, "a", event="same"), turn=fake_turn))
    assert n["runs"] == 1


def test_pairing_policy_sends_code_once_and_never_runs(env):
    m, acct = env["m"], env["acct"]
    store.update_account(acct["id"], config={"dmPolicy": "pairing"})
    acct = store.get_account(acct["id"])

    async def boom(a, messages, session_id):
        raise AssertionError("must not run")

    ad = asyncio.run(_run(m, acct, _msg(acct, "hi", event="1"), _msg(acct, "hi again", event="2"), turn=boom))
    reqs = store.list_pairing_requests(acct["id"])
    assert len(reqs) == 1 and reqs[0]["senderId"] == "628111"
    assert len(ad.sent) == 1 and reqs[0]["code"] in ad.sent[0][1]
    store.resolve_pairing(reqs[0]["id"], approve=True)
    acct = store.get_account(acct["id"])
    ran = {}

    async def ok(a, messages, session_id):
        ran["yes"] = True
        return "welcome", None

    ad = asyncio.run(_run(m, acct, _msg(acct, "now?", event="3"), turn=ok))
    assert ran.get("yes") and ad.sent[-1][1] == "welcome"


def test_allowlist_dm_denies_unknown_and_allows_listed(env):
    m, acct = env["m"], env["acct"]
    store.update_account(acct["id"], config={"dmPolicy": "allowlist", "allowFrom": ["628222"]})
    acct = store.get_account(acct["id"])
    ran = []

    async def t(a, messages, session_id):
        ran.append(session_id)
        return "ok", None

    ad = asyncio.run(_run(m, acct, _msg(acct, "x", sender="628111", event="1"),
                          _msg(acct, "y", sender="628222", event="2"), turn=t))
    assert ran == [f"fake:{acct['id']}:628222"]
    texts = [s[1] for s in ad.sent]
    assert mgr_mod.NOT_ALLOWED_TEXT in texts and "ok" in texts


def test_group_mention_policy(env):
    m, acct = env["m"], env["acct"]
    ran = []

    async def t(a, messages, session_id):
        ran.append(messages[-1]["content"])
        return "ok", None

    asyncio.run(_run(m, acct,
                     _msg(acct, "no mention", conv="g1@g.us", kind="group", event="1"),
                     _msg(acct, "with mention", conv="g1@g.us", kind="group", event="2", mentioned=True),
                     turn=t))
    assert ran == ["[Bob] with mention"]


def test_slash_commands_and_binding_override(env):
    m, acct = env["m"], env["acct"]
    other = agent_store.create_agent({"name": "Other", "persona": "p"})
    store.upsert_binding(acct["id"], "628111", other["id"])
    seen = {}

    async def t(a, messages, session_id):
        seen["agent"] = a["id"]
        return "ok", None

    ad = asyncio.run(_run(m, acct, _msg(acct, "/status", event="s"), turn=t))
    assert "Other" in ad.sent[-1][1] and "agent" not in seen
    ad = asyncio.run(_run(m, acct, _msg(acct, "talk", event="t"), turn=t))
    assert seen["agent"] == other["id"]
    ad = asyncio.run(_run(m, acct, _msg(acct, "/reset", event="r"), turn=t))
    assert ad.sent[-1][1] == "Conversation reset."
    assert chat_store.get_session(f"fake:{acct['id']}:628111") is None


def test_error_and_timeout_turn_into_friendly_replies(env):
    m, acct = env["m"], env["acct"]

    async def bad(a, messages, session_id):
        return "", "model exploded"

    ad = asyncio.run(_run(m, acct, _msg(acct, "x", event="1"), turn=bad))
    assert ad.sent[-1][1] == mgr_mod.RUN_ERROR_TEXT
    store.update_account(acct["id"], config={"runTimeoutS": 1})
    acct = store.get_account(acct["id"])

    async def slow(a, messages, session_id):
        await asyncio.sleep(2)
        return "late", None

    ad = asyncio.run(_run(m, acct, _msg(acct, "y", event="2"), turn=slow))
    assert ad.sent[-1][1] == mgr_mod.RUN_ERROR_TEXT
    assert channel_store.list_deliveries()[0]["status"] == "sent"


def test_attachments_are_described_to_the_model(env):
    m, acct = env["m"], env["acct"]
    seen = {}

    async def t(a, messages, session_id):
        seen["content"] = messages[-1]["content"]
        return "ok", None

    msg = _msg(acct, "see this", event="a")
    msg.attachments = [Attachment(path="/tmp/x/photo.jpg", mime="image/jpeg", name="photo.jpg", kind="image", caption="cap")]
    asyncio.run(_run(m, acct, msg, turn=t))
    assert "see this" in seen["content"] and "photo.jpg" in seen["content"] and "cap" in seen["content"]


def test_proactive_send_has_self_send_guard_and_audit(env):
    m, acct = env["m"], env["acct"]
    asyncio.run(m.start_account(acct["id"]))
    rec = asyncio.run(m.send(acct["id"], "me-id", "hi", source="tool"))
    assert rec["status"] == "dropped"
    rec = asyncio.run(m.send(acct["id"], "628333", "hi", source="tool"))
    assert rec["status"] == "sent" and m.adapter(acct["id"]).sent[-1][0] == "628333"
    rec = asyncio.run(m.send("missing", "628333", "hi"))
    assert rec["status"] == "failed"


def test_disabled_account_ignores_everything(env):
    m, acct = env["m"], env["acct"]
    store.update_account(acct["id"], enabled=False)

    async def boom(a, messages, session_id):
        raise AssertionError("must not run")

    mgr_mod.execute_turn = boom
    asyncio.run(m.handle_inbound(_msg(acct, "x")))
