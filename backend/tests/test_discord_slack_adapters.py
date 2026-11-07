"""Discord + Slack adapters — event parsing and a round-trip over a fake
WebSocket and a mock HTTP transport (no network)."""

import asyncio
import json

import httpx

import config
from channels import discord, slack
from channels.base import Attachment


class FakeWS:
    """Scripted websocket: yields queued frames, then waits until closed."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = asyncio.Event()

    async def recv(self):
        if self.frames:
            await asyncio.sleep(0)
            return json.dumps(self.frames.pop(0))
        await self.closed.wait()
        raise ConnectionError("closed")

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed.set()


async def _wait(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


# ─── Discord ────────────────────────────────────────────────────────────────

def test_discord_parse():
    bot = "42"
    dm = discord.parse_message_create("a", {"id": "m1", "channel_id": "c1", "content": "hi",
                                            "author": {"id": "7", "username": "bob", "global_name": "Bob"}}, bot_id=bot)
    assert dm.kind == "dm" and dm.mentioned and dm.sender_name == "Bob" and dm.session_key == "discord:a:c1"
    g = discord.parse_message_create("a", {"id": "m2", "channel_id": "c2", "guild_id": "g", "content": "<@42> status?",
                                           "mentions": [{"id": "42"}], "author": {"id": "7", "username": "bob"}}, bot_id=bot)
    assert g.kind == "group" and g.mentioned and g.text == "status?"
    silent = discord.parse_message_create("a", {"id": "m3", "channel_id": "c2", "guild_id": "g", "content": "chatter",
                                                "author": {"id": "7", "username": "bob"}}, bot_id=bot)
    assert silent.kind == "group" and not silent.mentioned
    assert discord.parse_message_create("a", {"id": "m4", "channel_id": "c1", "content": "x", "author": {"id": "9", "bot": True}}, bot_id=bot) is None
    rep = discord.parse_message_create("a", {"id": "m5", "channel_id": "c2", "guild_id": "g", "content": "yes",
                                             "referenced_message": {"id": "m0", "author": {"id": "42"}},
                                             "author": {"id": "7", "username": "bob"}}, bot_id=bot)
    assert rep.mentioned and rep.reply_to == "m0"


def test_discord_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"IMG")
        body = {}
        if request.content and request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content)
        calls.append((request.method, path, body, dict(request.headers)))
        if path.endswith("/gateway/bot"):
            return httpx.Response(200, json={"url": "wss://gw.example"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"id": "out-1"})
        if path.endswith("/typing"):
            return httpx.Response(204)
        return httpx.Response(404)

    ws = FakeWS([
        {"op": 10, "d": {"heartbeat_interval": 60000}},
        {"op": 0, "t": "READY", "s": 1, "d": {"user": {"id": "42", "username": "worker"},
                                            "session_id": "sess-1", "resume_gateway_url": "wss://resume.example"}},
        {"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": {"id": "1001", "channel_id": "c1", "content": "hello",
                                                     "author": {"id": "7", "username": "bob"},
                                                     "attachments": [{"url": "https://cdn.example/a.png", "filename": "a.png",
                                                                      "content_type": "image/png", "size": 3}]}},
    ])
    monkeypatch.setattr(discord.DiscordAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    async def connect(url):
        assert url.startswith("wss://gw.example")
        return ws

    async def main():
        inbound = []

        async def on_inbound(m):
            inbound.append(m)

        ad = discord.DiscordAdapter({"id": "acct", "kind": "discord", "config": {}}, {"botToken": "T"},
                                    on_inbound=on_inbound, data_dir=str(tmp_path / "d"))
        ad.ws_connect = connect
        await ad.start()
        assert await _wait(lambda: len(inbound) == 1)
        assert ad.status()["state"] == "open" and ad.status()["me"] == "42"
        assert ws.sent[0]["op"] == 2 and ws.sent[0]["d"]["intents"] == discord.INTENTS
        assert any(h.get("authorization") == "Bot T" for _, _, _, h in calls)
        m = inbound[0]
        assert m.text == "hello" and not m.attachments and m.attachment_loader   # fetched after policy
        await m.load_attachments()
        assert m.attachments and m.attachments[0].kind == "image"
        assert open(m.attachments[0].path, "rb").read() == b"IMG"
        mid = await ad.send("c1", "reply @everyone", reply_to="1001")
        assert mid == "out-1"
        sent = [c for c in calls if c[1].endswith("/channels/c1/messages")][-1]
        assert sent[2]["content"] == "reply @everyone" and sent[2]["message_reference"]["message_id"] == "1001"
        assert sent[2]["allowed_mentions"] == {"parse": [], "replied_user": False}   # never pings
        assert ad._session_id == "sess-1" and ad._resume_url == "wss://resume.example"
        await ad.typing("c1", True)
        assert any(c[1].endswith("/typing") for c in calls)
        await ad.stop()
        assert ad.status()["state"] == "off"

    asyncio.run(main())


# ─── Slack ──────────────────────────────────────────────────────────────────

def test_slack_parse():
    bot = "U42"
    dm = slack.parse_event("a", {"type": "message", "channel": "D1", "user": "U7", "text": "hi", "ts": "1.1"}, bot_id=bot)
    assert dm.kind == "dm" and dm.mentioned and dm.event_id == "D1:1.1" and dm.session_key == "slack:a:D1"
    men = slack.parse_event("a", {"type": "message", "channel": "C1", "user": "U7", "text": "<@U42> help", "ts": "2.2"}, bot_id=bot)
    assert men.kind == "group" and men.mentioned and men.text == "help" and men.raw["threadTs"] == "2.2"
    # app_mention duplicates the message event (without files) → ignored
    assert slack.parse_event("a", {"type": "app_mention", "channel": "C1", "user": "U7", "text": "<@U42> help", "ts": "2.2"}, bot_id=bot) is None
    assert slack.escape_text("a <!channel> & b") == "a &lt;!channel&gt; &amp; b"
    thr = slack.parse_event("a", {"type": "message", "channel": "C1", "user": "U7", "text": "more", "ts": "3.3", "thread_ts": "2.2"}, bot_id=bot)
    assert thr.reply_to == "2.2" and thr.raw["threadTs"] == "2.2" and not thr.mentioned
    assert slack.parse_event("a", {"type": "message", "channel": "C1", "bot_id": "B1", "text": "x", "ts": "4"}, bot_id=bot) is None
    assert slack.parse_event("a", {"type": "message", "subtype": "channel_join", "channel": "C1", "user": "U7", "text": "joined", "ts": "5"}, bot_id=bot) is None


def test_slack_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "files.example":
            assert request.headers.get("authorization") == "Bearer xoxb"
            return httpx.Response(200, content=b"PDF")
        body = {}
        if request.content and request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content)
        calls.append((path, body, dict(request.headers)))
        if path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": "U42", "user": "worker"})
        if path.endswith("/apps.connections.open"):
            assert request.headers.get("authorization") == "Bearer xapp"
            return httpx.Response(200, json={"ok": True, "url": "wss://sock.example"})
        if path.endswith("/chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "ts": "9.9"})
        if path.endswith("/users.info"):
            return httpx.Response(200, json={"ok": True, "user": {"name": "bob", "real_name": "Bob Smith",
                                                                   "profile": {"display_name": "bobby"}}})
        return httpx.Response(404)

    ws = FakeWS([
        {"type": "hello"},
        {"type": "events_api", "envelope_id": "e1", "payload": {"event": {
            "type": "message", "channel": "C1", "user": "U7", "text": "<@U42> read this", "ts": "5.5",
            "files": [{"url_private_download": "https://files.example/x.pdf", "name": "x.pdf", "mimetype": "application/pdf", "size": 3}]}}},
    ])
    monkeypatch.setattr(slack.SlackAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    async def connect(url):
        assert url == "wss://sock.example"
        return ws

    async def main():
        inbound = []

        async def on_inbound(m):
            inbound.append(m)

        ad = slack.SlackAdapter({"id": "acct", "kind": "slack", "config": {}}, {"appToken": "xapp", "botToken": "xoxb"},
                                on_inbound=on_inbound, data_dir=str(tmp_path / "d"))
        ad.ws_connect = connect
        await ad.start()
        assert await _wait(lambda: len(inbound) == 1)
        assert ad.status()["state"] == "open" and ad.status()["me"] == "U42"
        assert ws.sent and ws.sent[0] == {"envelope_id": "e1"}          # acked immediately
        m = inbound[0]
        assert m.text == "read this" and m.sender_name == "bobby"     # resolved via users.info
        assert not m.attachments and m.attachment_loader
        await m.load_attachments()
        assert m.attachments[0].kind == "document"
        assert open(m.attachments[0].path, "rb").read() == b"PDF"
        ts = await ad.send("C1", "answer <!channel> & co", reply_to=m.event_id)
        assert ts == "9.9"
        post = [c for c in calls if c[0].endswith("/chat.postMessage")][-1][1]
        assert post == {"channel": "C1", "text": "answer &lt;!channel&gt; &amp; co", "thread_ts": "5.5"}   # threaded + escaped
        await ad.send("D1", "dm reply", reply_to="D1:1.1")
        post = [c for c in calls if c[0].endswith("/chat.postMessage")][-1][1]
        assert "thread_ts" not in post
        await ad.send("C1", "", attachments=[Attachment(path=str(tmp_path / "d" / "none.txt"), name="none.txt")])
        post = [c for c in calls if c[0].endswith("/chat.postMessage")][-1][1]
        assert "not supported" in post["text"]
        await ad.stop()

    asyncio.run(main())


def test_slack_missing_tokens_and_discord_bad_token(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)

    async def main():
        s = slack.SlackAdapter({"id": "a", "kind": "slack", "config": {}}, {"botToken": "x"},
                               on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        await s.start()
        assert s.status()["state"] == "error" and "appToken" in s.status()["error"]
        monkeypatch.setattr(discord.DiscordAdapter, "client_factory",
                            staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                                lambda r: httpx.Response(401, json={"message": "401: Unauthorized"})))))
        d = discord.DiscordAdapter({"id": "b", "kind": "discord", "config": {}}, {"botToken": "bad"},
                                   on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        await d.start()
        assert await _wait(lambda: d.status()["state"] == "error")
        assert "gateway/bot 401" in d.status()["error"]
        await d.stop()

    asyncio.run(main())


class _Closed(Exception):
    """Mimics websockets.ConnectionClosed: carries the close frame."""
    def __init__(self, code, reason=""):
        super().__init__(f"closed {code}")
        self.rcvd = type("Close", (), {"code": code, "reason": reason})()


class ClosingWS(FakeWS):
    """Scripted frames, then raises a close with a code."""
    def __init__(self, frames, code):
        super().__init__(frames)
        self.code = code

    async def recv(self):
        if self.frames:
            await asyncio.sleep(0)
            return json.dumps(self.frames.pop(0))
        raise _Closed(self.code, "boom")


def test_discord_fatal_close_code_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(discord.DiscordAdapter, "client_factory", staticmethod(lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "wss://gw.example"})))))
    connects = []

    async def connect(url):
        connects.append(url)
        return ClosingWS([{"op": 10, "d": {"heartbeat_interval": 60000}}], 4014)

    async def main():
        ad = discord.DiscordAdapter({"id": "a", "kind": "discord", "config": {}}, {"botToken": "T"},
                                    on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        ad.ws_connect = connect
        await ad.start()
        assert await _wait(lambda: ad.status()["state"] == "error")
        assert "4014" in ad.status()["error"] and "Message Content" in ad.status()["error"]
        await asyncio.sleep(0.3)
        assert len(connects) == 1          # no reconnect loop on a config error
        await ad.stop()

    asyncio.run(main())


def test_discord_op7_resumes_with_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(discord.DiscordAdapter, "client_factory", staticmethod(lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "wss://gw.example"})))))
    sockets = []

    async def connect(url):
        if not sockets:
            ws = FakeWS([{"op": 10, "d": {"heartbeat_interval": 60000}},
                         {"op": 0, "t": "READY", "s": 5, "d": {"user": {"id": "42"}, "session_id": "S",
                                                               "resume_gateway_url": "wss://resume.example"}},
                         {"op": 7}])
        else:
            assert url.startswith("wss://resume.example")
            ws = FakeWS([{"op": 10, "d": {"heartbeat_interval": 60000}}, {"op": 0, "t": "RESUMED", "s": 6, "d": {}}])
        sockets.append(ws)
        return ws

    async def main():
        ad = discord.DiscordAdapter({"id": "a", "kind": "discord", "config": {}}, {"botToken": "T"},
                                    on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        ad.ws_connect = connect
        await ad.start()
        assert await _wait(lambda: len(sockets) == 2 and ad.status().get("detail") == "gateway resumed")
        resume = sockets[1].sent[0]
        assert resume["op"] == 6 and resume["d"] == {"token": "T", "session_id": "S", "seq": 5}
        await ad.stop()

    asyncio.run(main())


def test_discord_send_retries_after_429(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    hits = []

    def handler(request):
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "ok-2"})
    monkeypatch.setattr(discord.DiscordAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    async def main():
        ad = discord.DiscordAdapter({"id": "a", "kind": "discord", "config": {}}, {"botToken": "T"},
                                    on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        ad._set_status("open")
        assert await ad.send("c1", "hi") == "ok-2"
        assert len(hits) == 2

    asyncio.run(main())


def test_slack_disconnect_reconnects_promptly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)

    def handler(request):
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": "U42", "user": "w"})
        if request.url.path.endswith("/apps.connections.open"):
            return httpx.Response(200, json={"ok": True, "url": "wss://sock.example"})
        return httpx.Response(404)
    monkeypatch.setattr(slack.SlackAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    sockets = []

    async def connect(url):
        ws = FakeWS([{"type": "hello"}, {"type": "disconnect", "reason": "refresh_requested"}] if not sockets
                    else [{"type": "hello"}])
        sockets.append(ws)
        return ws

    async def main():
        ad = slack.SlackAdapter({"id": "a", "kind": "slack", "config": {}}, {"appToken": "x", "botToken": "y"},
                                on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        ad.ws_connect = connect
        await ad.start()
        assert await _wait(lambda: len(sockets) == 2 and ad.status()["state"] == "open", timeout=3.0)
        await ad.stop()

    asyncio.run(main())
