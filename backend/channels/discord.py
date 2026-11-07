"""Discord — a bot over the Gateway WebSocket + REST (no public URL).

One account = one bot token (write-only secret ``botToken``). The adapter dials
out to the Gateway, identifies with the message intents, heartbeats, resumes the
session after a drop when Discord allows it and re-identifies otherwise.
Inbound ``MESSAGE_CREATE`` events become normalized messages; replies go through
the REST API with a message reference so they thread visually under the message
they answer. Guild channels follow the account's group policy (``mention`` =
reply only when the bot is @mentioned); DMs follow the DM policy.

Safety: outbound messages never ping anyone (``allowed_mentions`` is empty), so
model text containing ``@everyone`` or a role mention is rendered inert.
Attachments are only downloaded once the manager has accepted the message.

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
from typing import Callable

import httpx

from channels import base
from channels.base import Attachment, InboundMessage, WebSocketAdapter, registry

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)   # GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT
MAX_MEDIA_BYTES = int(os.environ.get("VW_DISCORD_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))
MAX_TEXT = 1900   # Discord caps at 2000
NO_MENTIONS = {"parse": [], "replied_user": False}

# Gateway close codes after which reconnecting cannot help (bad token, bad or
# disallowed intents, bad API version). https://discord.com/developers/docs/topics/opcodes-and-status-codes
FATAL_CLOSE = {
    4004: "authentication failed: the bot token is wrong",
    4010: "invalid shard",
    4011: "sharding required: this bot is in too many guilds for one connection",
    4012: "invalid gateway API version",
    4013: "invalid intents",
    4014: "disallowed intents: enable the Message Content intent for this bot in the Developer Portal",
}


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
class DiscordAdapter(WebSocketAdapter):
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

    #: Test seam.
    client_factory: Callable[[], httpx.AsyncClient] = staticmethod(
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15)))

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._bot_id = ""
        self._seq: int | None = None
        self._session_id: str | None = None      # from READY; enables RESUME
        self._resume_url: str | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.secrets.get('botToken', '')}"}

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if not self.secrets.get("botToken"):
            self._set_status("error", error="missing bot token")
            return
        self._set_status("connecting")
        self._run(f"discord:{self.account.get('id')}", self._session)

    def _forget_session(self) -> None:
        self._session_id = None
        self._resume_url = None
        self._seq = None

    async def _session(self) -> None:
        resuming = bool(self._session_id and self._resume_url)
        async with self.client_factory() as c:
            if resuming:
                url = self._resume_url or ""
            else:
                r = await c.get(f"{API}/gateway/bot", headers=self._headers())
                if r.status_code != 200:
                    self._set_status("error", error=f"gateway/bot {r.status_code}: {r.text[:120]}")
                    raise RuntimeError("discord gateway lookup failed")
                url = r.json().get("url") or "wss://gateway.discord.gg"
            ws = await self._connect(f"{url.rstrip('/')}/?v=10&encoding=json")
            self._ws = ws
            heartbeat_task: asyncio.Task | None = None
            ready = False
            try:
                while not self._stop.is_set():
                    try:
                        raw = await ws.recv()
                    except Exception as exc:  # noqa: BLE001 — closed socket
                        code, reason = base.close_code(exc)
                        if code in FATAL_CLOSE:
                            self._forget_session()
                            self._set_status("error", error=f"gateway closed ({code}): {FATAL_CLOSE[code]}")
                            self._stop.set()            # no point retrying with the same config
                            return
                        raise RuntimeError(f"gateway closed ({code or '?'}) {reason}".strip()) from exc
                    msg = json.loads(raw)
                    op, t, d = msg.get("op"), msg.get("t"), msg.get("d")
                    if msg.get("s") is not None:
                        self._seq = msg["s"]
                    if op == 10:   # HELLO → identify (or resume) + heartbeat
                        interval = float((d or {}).get("heartbeat_interval", 41250)) / 1000.0
                        if resuming:
                            await ws.send(json.dumps({"op": 6, "d": {
                                "token": self.secrets.get("botToken", ""),
                                "session_id": self._session_id, "seq": self._seq}}))
                        else:
                            await ws.send(json.dumps({"op": 2, "d": {
                                "token": self.secrets.get("botToken", ""), "intents": INTENTS,
                                "properties": {"os": "virtual-worker", "browser": "virtual-worker", "device": "virtual-worker"}}}))
                        heartbeat_task = asyncio.get_event_loop().create_task(self._heartbeat(ws, interval))
                    elif op == 11:   # heartbeat ack
                        continue
                    elif op == 7:    # reconnect: return cleanly, the next session resumes
                        return
                    elif op == 9:    # invalid session; d == true means resumable
                        if not d:
                            self._forget_session()
                        raise RuntimeError("discord invalidated the session")
                    elif op == 0 and t == "READY":
                        user = (d or {}).get("user") or {}
                        self._bot_id = str(user.get("id") or "")
                        self._session_id = (d or {}).get("session_id") or None
                        self._resume_url = (d or {}).get("resume_gateway_url") or None
                        ready = True
                        self._set_status("open", me=self._bot_id, name=user.get("username"), detail="gateway connected")
                    elif op == 0 and t == "RESUMED":
                        ready = True
                        self._set_status("open", me=self._bot_id, detail="gateway resumed")
                    elif op == 0 and t == "MESSAGE_CREATE":
                        try:
                            await self._on_message(d or {})
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
                if resuming and not ready:
                    self._forget_session()      # resume rejected → identify fresh next time
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

    async def _on_message(self, d: dict) -> None:
        msg = parse_message_create(self.account.get("id", ""), d, bot_id=self._bot_id)
        if msg is None:
            return
        media = (msg.raw or {}).get("media") or []
        if media:
            async def load(media=media, caption=msg.text):
                out: list[Attachment] = []
                async with self.client_factory() as c:
                    for m in media:
                        path = await base.download_to_media_dir(
                            c, m["url"], data_dir=self.data_dir, cap=MAX_MEDIA_BYTES,
                            name=m.get("name") or "", mime=m.get("mime") or "", size=m.get("size") or 0)
                        if path:
                            out.append(base.attachment_from_media(path, m, caption))
                return out
            msg.attachment_loader = load
        await self.on_inbound(msg)

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        if self._status.get("state") != "open":
            raise RuntimeError(f"Discord bot is not connected (state: {self._status.get('state')})")
        channel_id = (target or "").strip()
        if not channel_id:
            raise RuntimeError("target channel id is required")
        last = "sent"
        url = f"{API}/channels/{channel_id}/messages"
        async with self.client_factory() as c:
            if text:
                body: dict = {"content": text, "allowed_mentions": NO_MENTIONS}
                if reply_to and reply_to.isdigit():
                    body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
                r = await base.post_with_retry(c, url, json=body, headers=self._headers())
                last = self._result_id(r)
            for a in attachments or []:
                mime = a.mime or mimetypes.guess_type(a.path)[0] or "application/octet-stream"
                with open(a.path, "rb") as fh:
                    r = await base.post_with_retry(
                        c, url, headers=self._headers(),
                        data={"payload_json": json.dumps({"content": a.caption or "", "allowed_mentions": NO_MENTIONS})},
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
