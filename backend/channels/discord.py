"""Discord — a bot over the Gateway WebSocket + REST (no public URL).

One account = one bot token (write-only secret ``botToken``). The adapter dials
out to the Gateway, identifies with the message intents, heartbeats, and
re-identifies with backoff on any drop. Inbound ``MESSAGE_CREATE`` events become
normalized messages; replies go through the REST API with a message reference so
they thread visually under the message they answer. Guild channels follow the
account's group policy (``mention`` = reply only when the bot is @mentioned);
DMs follow the DM policy.

Setup (Discord Developer Portal): create an application → Bot → copy the token,
enable the **Message Content** privileged intent, then invite the bot to a
server with the ``bot`` scope and *Send Messages* + *Read Message History*.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Callable

import httpx

from channels import base
from channels.base import Attachment, ChannelAdapter, InboundMessage, registry

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)   # GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT
MAX_MEDIA_BYTES = int(os.environ.get("VW_DISCORD_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))
MAX_TEXT = 1900   # Discord caps at 2000


def parse_message_create(account_id: str, d: dict, *, bot_id: str) -> InboundMessage | None:
    """Normalize a ``MESSAGE_CREATE`` payload. Bot/system messages are dropped."""
    author = d.get("author") or {}
    if author.get("bot") or d.get("webhook_id"):
        return None
    text = d.get("content") or ""
    is_dm = not d.get("guild_id")
    mentioned = is_dm or any(str(u.get("id")) == str(bot_id) for u in (d.get("mentions") or [])) \
        or f"<@{bot_id}>" in text or f"<@!{bot_id}>" in text
    text = re.sub(rf"<@!?{re.escape(str(bot_id))}>", "", text).strip()
    ref = d.get("referenced_message") or {}
    if ref and str((ref.get("author") or {}).get("id")) == str(bot_id):
        mentioned = True
    name = (author.get("global_name") or author.get("username") or "")
    media = [{"url": a.get("url"), "name": a.get("filename") or "", "mime": a.get("content_type") or "",
              "size": a.get("size") or 0} for a in (d.get("attachments") or []) if a.get("url")]
    if not text and not media:
        return None
    return InboundMessage(
        channel="discord", account_id=account_id, conversation_id=str(d.get("channel_id")),
        kind="dm" if is_dm else "group", sender_id=str(author.get("id") or ""), sender_name=name,
        text=text, reply_to=str(ref.get("id")) if ref else None, event_id=str(d.get("id") or ""),
        ts=d.get("timestamp") or "", mentioned=mentioned, from_me=str(author.get("id")) == str(bot_id),
        raw={"media": media, "guildId": d.get("guild_id")},
    )


@registry.register
class DiscordAdapter(ChannelAdapter):
    kind = "discord"
    label = "Discord"
    description = ("A Discord bot via the Gateway. Create an application in the Developer Portal, "
                   "copy the bot token, enable the Message Content intent, invite the bot. Dials out "
                   "— no public URL needed.")
    config_fields: list[dict] = []
    secret_fields = [{"key": "botToken", "label": "Bot token"}]
    capabilities = {"media": True, "typing": True, "groups": True, "threads": False,
                    "pairing": False, "webhook": False, "markdown": "discord"}
    max_text_len = MAX_TEXT

    #: Test seams.
    client_factory: Callable[[], httpx.AsyncClient] = staticmethod(
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15)))
    ws_connect = None   # async callable(url) -> websocket-like (recv/send/close); None = websockets.connect

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._bot_id = ""
        self._ws = None
        self._seq: int | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.secrets.get('botToken', '')}"}

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if not self.secrets.get("botToken"):
            self._set_status("error", error="missing bot token")
            return
        self._stop.clear()
        self._set_status("connecting")
        self._task = asyncio.get_event_loop().create_task(
            base.run_with_backoff(f"discord:{self.account.get('id')}", self._session, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            self._task = None
        self._set_status("off")

    async def _connect(self, url: str):
        if self.ws_connect is not None:
            return await self.ws_connect(url)
        import websockets
        return await websockets.connect(url, max_size=8 * 1024 * 1024)

    async def _session(self) -> None:
        async with self.client_factory() as c:
            r = await c.get(f"{API}/gateway/bot", headers=self._headers())
            if r.status_code != 200:
                self._set_status("error", error=f"gateway/bot {r.status_code}: {r.text[:120]}")
                raise RuntimeError("discord gateway lookup failed")
            url = r.json().get("url") or "wss://gateway.discord.gg"
            ws = await self._connect(f"{url}/?v=10&encoding=json")
            self._ws = ws
            heartbeat_task: asyncio.Task | None = None
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                    if msg.get("s") is not None:
                        self._seq = msg["s"]
                    if op == 10:   # HELLO → identify + heartbeat
                        interval = float((d or {}).get("heartbeat_interval", 41250)) / 1000.0
                        await ws.send(json.dumps({"op": 2, "d": {
                            "token": self.secrets.get("botToken", ""), "intents": INTENTS,
                            "properties": {"os": "virtual-worker", "browser": "virtual-worker", "device": "virtual-worker"}}}))
                        heartbeat_task = asyncio.get_event_loop().create_task(self._heartbeat(ws, interval))
                    elif op == 11:   # heartbeat ack
                        continue
                    elif op == 7 or op == 9:   # reconnect / invalid session
                        raise RuntimeError(f"discord asked to reconnect (op {op})")
                    elif op == 0 and t == "READY":
                        user = (d or {}).get("user") or {}
                        self._bot_id = str(user.get("id") or "")
                        self._set_status("open", me=self._bot_id, name=user.get("username"), detail="gateway connected")
                    elif op == 0 and t == "MESSAGE_CREATE":
                        try:
                            await self._on_message(c, d or {})
                        except Exception:  # noqa: BLE001
                            logger.debug("discord message handling failed", exc_info=True)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ws = None
                if not self._stop.is_set():
                    self._set_status("reconnecting", detail="gateway closed")

    async def _heartbeat(self, ws, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def _on_message(self, c: httpx.AsyncClient, d: dict) -> None:
        msg = parse_message_create(self.account.get("id", ""), d, bot_id=self._bot_id)
        if msg is None:
            return
        for media in (msg.raw or {}).get("media") or []:
            path = await self._download(c, media)
            if path:
                mime = media.get("mime") or ""
                msg.attachments.append(Attachment(path=path, mime=mime, name=media.get("name") or os.path.basename(path),
                                                  kind="image" if mime.startswith("image/") else "document", caption=msg.text))
        await self.on_inbound(msg)

    async def _download(self, c: httpx.AsyncClient, media: dict) -> str | None:
        if (media.get("size") or 0) > MAX_MEDIA_BYTES:
            return None
        media_dir = Path(self.data_dir) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(media.get("name") or "").suffix or mimetypes.guess_extension(media.get("mime") or "") or ""
        out = media_dir / f"{uuid.uuid4().hex[:12]}{ext}"
        size = 0
        async with c.stream("GET", media["url"]) as r:
            if r.status_code != 200:
                return None
            with open(out, "wb") as fh:
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        fh.close(); out.unlink(missing_ok=True); return None
                    fh.write(chunk)
        return str(out)

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        if self._status.get("state") != "open":
            raise RuntimeError(f"Discord bot is not connected (state: {self._status.get('state')})")
        channel_id = (target or "").strip()
        if not channel_id:
            raise RuntimeError("target channel id is required")
        last = "sent"
        async with self.client_factory() as c:
            if text:
                body: dict = {"content": text}
                if reply_to and reply_to.isdigit():
                    body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
                r = await c.post(f"{API}/channels/{channel_id}/messages", json=body, headers=self._headers())
                last = self._result_id(r)
            for a in attachments or []:
                mime = a.mime or mimetypes.guess_type(a.path)[0] or "application/octet-stream"
                with open(a.path, "rb") as fh:
                    r = await c.post(f"{API}/channels/{channel_id}/messages", headers=self._headers(),
                                     data={"payload_json": json.dumps({"content": a.caption or ""})},
                                     files={"files[0]": (a.name or os.path.basename(a.path), fh, mime)})
                last = self._result_id(r)
        return last

    @staticmethod
    def _result_id(r: httpx.Response) -> str:
        if r.status_code >= 400:
            raise RuntimeError(f"Discord API {r.status_code}: {r.text[:200]}")
        try:
            return str(r.json().get("id") or "sent")
        except ValueError:
            return "sent"

    async def typing(self, target: str, on: bool = True) -> None:
        if not on or self._status.get("state") != "open":
            return
        try:
            async with self.client_factory() as c:
                await c.post(f"{API}/channels/{target}/typing", headers=self._headers())
        except Exception:  # noqa: BLE001
            pass

    def self_ids(self) -> set[str]:
        return {self._bot_id} if self._bot_id else set()
