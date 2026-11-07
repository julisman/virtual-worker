"""Telegram — a bot over the official Bot API with long polling (no public URL).

The simplest transport: pure ``httpx``, dials out via ``getUpdates`` with a
30-second long poll, so a laptop behind NAT works. One account = one bot token
(write-only secret ``botToken``). Groups: the bot must be added to the group;
with the default ``mention`` policy it replies when @mentioned or when someone
replies to one of its messages (Telegram's privacy mode hides other messages
unless the bot is an admin).

Media (photos, documents, voice, audio, video) is downloaded via ``getFile``
into the account's media dir, subject to the size cap. Replies are plain text
(no parse_mode) so model output can never break Telegram's markup rules.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Callable

import httpx

from channels import base
from channels.base import Attachment, ChannelAdapter, InboundMessage, registry

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_MEDIA_BYTES = int(os.environ.get("VW_TELEGRAM_MAX_MEDIA_BYTES", str(20 * 1024 * 1024)))
POLL_TIMEOUT_S = 30


def parse_update(account_id: str, update: dict, *, bot_id: str, bot_username: str) -> InboundMessage | None:
    """Normalize one Bot API update (``message`` only) into an InboundMessage.
    Media is left as ``raw["media"]`` for the adapter to download."""
    m = update.get("message") or update.get("channel_post")
    if not m:
        return None
    chat = m.get("chat") or {}
    sender = m.get("from") or {}
    chat_type = chat.get("type") or "private"
    kind = "dm" if chat_type == "private" else "group"
    text = m.get("text") or m.get("caption") or ""
    uname = (bot_username or "").lower()
    mentioned = bool(uname) and f"@{uname}" in text.lower()
    reply = m.get("reply_to_message") or {}
    if reply and str((reply.get("from") or {}).get("id")) == str(bot_id):
        mentioned = True
    if kind == "dm":
        mentioned = True
    if mentioned and uname:
        # Strip the handle so the model sees a clean request.
        import re
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.I).strip()
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or sender.get("username") or ""
    media = None
    if m.get("photo"):
        best = m["photo"][-1]
        media = {"file_id": best.get("file_id"), "kind": "image", "mime": "image/jpeg",
                 "name": "", "size": best.get("file_size") or 0}
    elif m.get("document"):
        d = m["document"]
        media = {"file_id": d.get("file_id"), "kind": "document", "mime": d.get("mime_type") or "",
                 "name": d.get("file_name") or "", "size": d.get("file_size") or 0}
    elif m.get("voice") or m.get("audio"):
        d = m.get("voice") or m.get("audio")
        media = {"file_id": d.get("file_id"), "kind": "audio", "mime": d.get("mime_type") or "audio/ogg",
                 "name": d.get("file_name") or "", "size": d.get("file_size") or 0}
    elif m.get("video"):
        d = m["video"]
        media = {"file_id": d.get("file_id"), "kind": "video", "mime": d.get("mime_type") or "video/mp4",
                 "name": d.get("file_name") or "", "size": d.get("file_size") or 0}
    if not text and not media:
        return None  # joins, pins, stickers, locations… not handled in v1
    return InboundMessage(
        channel="telegram", account_id=account_id, conversation_id=str(chat.get("id")),
        kind=kind, sender_id=str(sender.get("id") or ""), sender_name=name, text=text,
        reply_to=str(reply.get("message_id")) if reply else None,
        event_id=f"{chat.get('id')}:{m.get('message_id')}",
        ts=str(m.get("date") or ""), mentioned=mentioned,
        from_me=str(sender.get("id")) == str(bot_id),
        raw={"media": media, "messageId": m.get("message_id"), "chatTitle": chat.get("title")},
    )


@registry.register
class TelegramAdapter(ChannelAdapter):
    kind = "telegram"
    label = "Telegram"
    description = ("A Telegram bot via the official Bot API. Create one with @BotFather, paste the "
                   "token. Long polling — no public URL needed.")
    config_fields: list[dict] = []
    secret_fields = [{"key": "botToken", "label": "Bot token (from @BotFather)"}]
    capabilities = {"media": True, "typing": True, "groups": True, "threads": False,
                    "pairing": False, "webhook": False, "markdown": False}
    max_text_len = 4000

    #: Test seam: a factory returning an ``httpx.AsyncClient``.
    client_factory: Callable[[], httpx.AsyncClient] = staticmethod(
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT_S + 10, connect=15)))

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._offset: int | None = None
        self._bot_id = ""
        self._username = ""

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.secrets.get('botToken', '')}/{method}"

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if not self.secrets.get("botToken"):
            self._set_status("error", error="missing bot token")
            return
        self._stop.clear()
        self._set_status("connecting")
        self._task = asyncio.get_event_loop().create_task(
            base.run_with_backoff(f"telegram:{self.account.get('id')}", self._poll_forever, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            self._task = None
        self._set_status("off")

    async def _poll_forever(self) -> None:
        async with self.client_factory() as c:
            r = await c.get(self._url("getMe"))
            data = r.json() if r.status_code == 200 else {}
            if not data.get("ok"):
                self._set_status("error", error=f"getMe failed: {r.status_code} {r.text[:120]}")
                raise RuntimeError("telegram getMe failed")
            me = data["result"]
            self._bot_id = str(me.get("id"))
            self._username = me.get("username") or ""
            self._set_status("open", me=self._bot_id, name=f"@{self._username}" if self._username else me.get("first_name"),
                             detail="long polling")
            while not self._stop.is_set():
                params: dict = {"timeout": POLL_TIMEOUT_S, "allowed_updates": '["message"]'}
                if self._offset is not None:
                    params["offset"] = self._offset
                r = await c.get(self._url("getUpdates"), params=params)
                if r.status_code == 409:
                    self._set_status("error", error="another process is polling this bot (409)")
                    raise RuntimeError("telegram getUpdates conflict")
                if r.status_code != 200:
                    raise RuntimeError(f"getUpdates {r.status_code}")
                updates = r.json().get("result") or []
                for upd in updates:
                    self._offset = int(upd.get("update_id", 0)) + 1
                    try:
                        await self._on_update(c, upd)
                    except Exception:  # noqa: BLE001
                        logger.debug("telegram update handling failed", exc_info=True)
                # Always yield; a server that answers instantly must not hot-loop.
                await asyncio.sleep(0 if updates else 0.25)

    async def _on_update(self, c: httpx.AsyncClient, upd: dict) -> None:
        msg = parse_update(self.account.get("id", ""), upd, bot_id=self._bot_id, bot_username=self._username)
        if msg is None:
            return
        media = (msg.raw or {}).get("media")
        if media and media.get("file_id"):
            async def load(media=media, caption=msg.text):
                async with self.client_factory() as cc:
                    path = await self._download(cc, media)
                return [base.attachment_from_media(path, media, caption)] if path else []
            msg.attachment_loader = load
        await self.on_inbound(msg)

    async def _download(self, c: httpx.AsyncClient, media: dict) -> str | None:
        if (media.get("size") or 0) > MAX_MEDIA_BYTES:
            return None
        r = await c.get(self._url("getFile"), params={"file_id": media["file_id"]})
        if r.status_code != 200 or not r.json().get("ok"):
            return None
        fpath = r.json()["result"].get("file_path") or ""
        return await base.download_to_media_dir(
            c, f"{API}/file/bot{self.secrets.get('botToken', '')}/{fpath}",
            data_dir=self.data_dir, cap=MAX_MEDIA_BYTES, name=media.get("name") or fpath,
            mime=media.get("mime") or "")

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        if self._status.get("state") != "open":
            raise RuntimeError(f"Telegram bot is not connected (state: {self._status.get('state')})")
        chat_id = (target or "").strip()
        if not chat_id:
            raise RuntimeError("target chat id is required")
        last = "sent"
        async with self.client_factory() as c:
            if text:
                body: dict = {"chat_id": chat_id, "text": text}
                if reply_to and reply_to.isdigit():
                    body["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
                r = await c.post(self._url("sendMessage"), json=body)
                last = self._result_id(r)
            for a in attachments or []:
                method, field = ("sendPhoto", "photo") if a.kind == "image" else ("sendDocument", "document")
                mime = a.mime or mimetypes.guess_type(a.path)[0] or "application/octet-stream"
                with open(a.path, "rb") as fh:
                    r = await c.post(self._url(method), data={"chat_id": chat_id, **({"caption": a.caption} if a.caption else {})},
                                     files={field: (a.name or os.path.basename(a.path), fh, mime)})
                last = self._result_id(r)
        return last

    @staticmethod
    def _result_id(r: httpx.Response) -> str:
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"Telegram API {r.status_code}: {(data.get('description') or r.text)[:200]}")
        return str((data.get("result") or {}).get("message_id") or "sent")

    async def typing(self, target: str, on: bool = True) -> None:
        if not on or self._status.get("state") != "open":
            return
        try:
            async with self.client_factory() as c:
                await c.post(self._url("sendChatAction"), json={"chat_id": target, "action": "typing"})
        except Exception:  # noqa: BLE001
            pass

    def self_ids(self) -> set[str]:
        return {self._bot_id} if self._bot_id else set()
