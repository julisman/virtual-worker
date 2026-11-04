"""Telegram adapter — update parsing + the Bot API round-trip over a mock transport."""

import asyncio
import json

import httpx

import config
from channels import telegram
from channels.base import Attachment


def _upd(**m):
    base = {"message_id": 7, "date": 1700000000, "chat": {"id": 12345, "type": "private"},
            "from": {"id": 999, "first_name": "Bob", "last_name": "K", "username": "bobk"}}
    base.update(m)
    return {"update_id": 1, "message": base}


def test_parse_dm_group_mention_reply_and_media():
    dm = telegram.parse_update("a", _upd(text="hi"), bot_id="42", bot_username="MyBot")
    assert dm.kind == "dm" and dm.mentioned and dm.sender_id == "999" and dm.sender_name == "Bob K"
    assert dm.conversation_id == "12345" and dm.event_id == "12345:7" and dm.text == "hi"
    grp = telegram.parse_update("a", _upd(text="hey @mybot what's up", chat={"id": -100, "type": "supergroup", "title": "Ops"}),
                                bot_id="42", bot_username="MyBot")
    assert grp.kind == "group" and grp.mentioned and "@" not in grp.text and "what's up" in grp.text
    silent = telegram.parse_update("a", _upd(text="no bot here", chat={"id": -100, "type": "group"}), bot_id="42", bot_username="MyBot")
    assert silent.kind == "group" and not silent.mentioned
    rep = telegram.parse_update("a", _upd(text="yes", chat={"id": -100, "type": "group"},
                                          reply_to_message={"message_id": 3, "from": {"id": 42}}), bot_id="42", bot_username="MyBot")
    assert rep.mentioned and rep.reply_to == "3"
    photo = telegram.parse_update("a", _upd(photo=[{"file_id": "s", "file_size": 10}, {"file_id": "L", "file_size": 100}], caption="look"),
                                  bot_id="42", bot_username="MyBot")
    assert photo.raw["media"]["file_id"] == "L" and photo.raw["media"]["kind"] == "image" and photo.text == "look"
    assert telegram.parse_update("a", {"update_id": 2, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"},
                                                                    "from": {"id": 5}, "new_chat_members": []}},
                                 bot_id="42", bot_username="MyBot") is None
    own = telegram.parse_update("a", _upd(text="echo", **{"from": {"id": 42, "first_name": "Bot"}}), bot_id="42", bot_username="MyBot")
    assert own.from_me


def test_round_trip_over_mock_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    calls: list[tuple[str, dict]] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/file/"):
            return httpx.Response(200, content=b"PNGDATA")
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {"_multipart": True}
        calls.append((method, {**dict(request.url.params), **body}))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 42, "username": "MyBot", "first_name": "My"}})
        if method == "getUpdates":
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"ok": True, "result": [
                    _upd(text="hello"),
                    {"update_id": 2, "message": {"message_id": 8, "date": 1, "chat": {"id": 12345, "type": "private"},
                                                 "from": {"id": 999, "first_name": "Bob"},
                                                 "photo": [{"file_id": "F1", "file_size": 7}], "caption": "pic"}},
                ]})
            return httpx.Response(200, json={"ok": True, "result": []})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/f1.jpg"}})
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        if method == "sendChatAction":
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False, "description": "unknown"})

    monkeypatch.setattr(telegram.TelegramAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    async def main():
        inbound = []

        async def on_inbound(m):
            inbound.append(m)

        ad = telegram.TelegramAdapter({"id": "acct", "kind": "telegram", "config": {}}, {"botToken": "T"},
                                      on_inbound=on_inbound, data_dir=str(tmp_path / "d"))
        await ad.start()
        for _ in range(50):
            if len(inbound) >= 2:
                break
            await asyncio.sleep(0.05)
        assert ad.status()["state"] == "open" and ad.status()["me"] == "42" and ad.status()["name"] == "@MyBot"
        assert ad.self_ids() == {"42"}
        assert inbound[0].text == "hello" and inbound[0].session_key == "telegram:acct:12345"
        assert inbound[1].attachments and inbound[1].attachments[0].kind == "image"
        assert open(inbound[1].attachments[0].path, "rb").read() == b"PNGDATA"
        # offset advanced past the two updates
        later = [c for c in calls if c[0] == "getUpdates" and c[1].get("offset")]
        assert later and later[0][1]["offset"] == "3"
        mid = await ad.send("12345", "reply text", reply_to="7")
        assert mid == "77"
        sm = [c for c in calls if c[0] == "sendMessage"][-1][1]
        assert sm["chat_id"] == "12345" and sm["reply_parameters"]["message_id"] == 7
        f = tmp_path / "doc.txt"; f.write_text("x")
        await ad.send("12345", "", attachments=[Attachment(path=str(f), kind="document", name="doc.txt")])
        assert [c for c in calls if c[0] == "sendDocument"]
        await ad.typing("12345", True)
        assert [c for c in calls if c[0] == "sendChatAction"]
        await ad.stop()
        assert ad.status()["state"] == "off"

    asyncio.run(main())


def test_bad_token_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(telegram.TelegramAdapter, "client_factory",
                        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                            lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})))))

    async def main():
        ad = telegram.TelegramAdapter({"id": "acct", "kind": "telegram", "config": {}}, {"botToken": "bad"},
                                      on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        await ad.start()
        for _ in range(40):
            if ad.status()["state"] == "error":
                break
            await asyncio.sleep(0.05)
        assert ad.status()["state"] == "error" and "getMe" in ad.status()["error"]
        await ad.stop()
        ad2 = telegram.TelegramAdapter({"id": "b", "kind": "telegram", "config": {}}, {}, on_inbound=lambda m: asyncio.sleep(0), data_dir=str(tmp_path))
        await ad2.start()
        assert ad2.status()["state"] == "error"

    asyncio.run(main())
