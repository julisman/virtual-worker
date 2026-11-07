"""Slack — a bot over Socket Mode (no public URL).

One account = an app-level token (``xapp-…``, secret ``appToken``) for the
Socket Mode WebSocket plus a bot token (``xoxb-…``, secret ``botToken``) for
the Web API. The adapter opens the socket, acknowledges every envelope
immediately, and turns ``message`` / ``app_mention`` events into normalized
messages. Replies go out with ``chat.postMessage``; in channels they land in
the thread of the message they answer, in DMs they are plain.

Setup (api.slack.com/apps): create an app → enable **Socket Mode** (creates the
app token with ``connections:write``) → Event Subscriptions: ``message.im``,
``message.channels``, ``app_mention`` → OAuth scopes: ``chat:write``,
``app_mentions:read``, ``im:history``, ``channels:history``, ``files:read`` →
install to the workspace and copy the bot token.
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

API = "https://slack.com/api"
MAX_MEDIA_BYTES = int(os.environ.get("VW_SLACK_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))
MAX_TEXT = 3500   # Slack caps at ~4000


def parse_event(account_id: str, ev: dict, *, bot_id: str) -> InboundMessage | None:
    """Normalize a ``message`` or ``app_mention`` event. Bot/system subtypes drop."""
    etype = ev.get("type")
    if etype not in ("message", "app_mention"):
        return None
    if ev.get("bot_id") or ev.get("subtype") in ("bot_message", "message_changed", "message_deleted",
                                                 "channel_join", "channel_leave"):
        return None
    channel = ev.get("channel") or ""
    is_dm = channel.startswith("D") or ev.get("channel_type") == "im"
    text = ev.get("text") or ""
    mentioned = is_dm or etype == "app_mention" or f"<@{bot_id}>" in text
    text = re.sub(rf"<@{re.escape(str(bot_id))}>", "", text).strip()
    user = str(ev.get("user") or "")
    media = [{"url": f.get("url_private_download") or f.get("url_private"), "name": f.get("name") or "",
              "mime": f.get("mimetype") or "", "size": f.get("size") or 0}
             for f in (ev.get("files") or []) if f.get("url_private_download") or f.get("url_private")]
    if not text and not media:
        return None
    ts = str(ev.get("ts") or "")
    return InboundMessage(
        channel="slack", account_id=account_id, conversation_id=channel,
        kind="dm" if is_dm else "group", sender_id=user, sender_name=user, text=text,
        reply_to=str(ev.get("thread_ts")) if ev.get("thread_ts") else None,
        event_id=f"{channel}:{ts}", ts=ts, mentioned=mentioned, from_me=user == str(bot_id),
        raw={"media": media, "threadTs": ev.get("thread_ts") or ts, "ts": ts},
    )


@registry.register
class SlackAdapter(ChannelAdapter):
    kind = "slack"
    label = "Slack"
    description = ("A Slack app over Socket Mode: an app-level token for the socket plus a bot "
                   "token for posting. Dials out — no public URL needed.")
    config_fields: list[dict] = []
    secret_fields = [{"key": "appToken", "label": "App-level token (xapp-…)"},
                     {"key": "botToken", "label": "Bot token (xoxb-…)"}]
    capabilities = {"media": True, "typing": False, "groups": True, "threads": True,
                    "pairing": False, "webhook": False, "markdown": "mrkdwn"}
    max_text_len = MAX_TEXT

    client_factory: Callable[[], httpx.AsyncClient] = staticmethod(
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15)))
    ws_connect = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._bot_id = ""
        self._ws = None
        self._threads: dict[str, str] = {}   # event_id → thread_ts to answer in

    def _bot_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secrets.get('botToken', '')}"}

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        missing = [k for k in ("appToken", "botToken") if not self.secrets.get(k)]
        if missing:
            self._set_status("error", error=f"missing: {', '.join(missing)}")
            return
        self._stop.clear()
        self._set_status("connecting")
        self._task = asyncio.get_event_loop().create_task(
            base.run_with_backoff(f"slack:{self.account.get('id')}", self._session, self._stop))

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
            r = await c.post(f"{API}/auth.test", headers=self._bot_headers())
            auth = r.json() if r.status_code == 200 else {}
            if not auth.get("ok"):
                self._set_status("error", error=f"auth.test failed: {auth.get('error') or r.status_code}")
                raise RuntimeError("slack auth.test failed")
            self._bot_id = str(auth.get("user_id") or "")
            r = await c.post(f"{API}/apps.connections.open",
                             headers={"Authorization": f"Bearer {self.secrets.get('appToken', '')}"})
            data = r.json() if r.status_code == 200 else {}
            if not data.get("ok") or not data.get("url"):
                self._set_status("error", error=f"apps.connections.open failed: {data.get('error') or r.status_code}")
                raise RuntimeError("slack socket open failed")
            ws = await self._connect(data["url"])
            self._ws = ws
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    env = json.loads(raw)
                    etype = env.get("type")
                    if etype == "hello":
                        self._set_status("open", me=self._bot_id, name=f"@{auth.get('user')}" if auth.get("user") else None,
                                         detail="socket mode connected")
                        continue
                    if env.get("envelope_id"):
                        await ws.send(json.dumps({"envelope_id": env["envelope_id"]}))
                    if etype == "disconnect":
                        raise RuntimeError(f"slack asked to reconnect ({(env.get('reason') or '')})")
                    if etype == "events_api":
                        ev = ((env.get("payload") or {}).get("event")) or {}
                        try:
                            await self._on_event(c, ev)
                        except Exception:  # noqa: BLE001
                            logger.debug("slack event handling failed", exc_info=True)
            finally:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ws = None
                if not self._stop.is_set():
                    self._set_status("reconnecting", detail="socket closed")

    async def _on_event(self, c: httpx.AsyncClient, ev: dict) -> None:
        msg = parse_event(self.account.get("id", ""), ev, bot_id=self._bot_id)
        if msg is None:
            return
        if msg.kind == "group":
            self._threads[msg.event_id] = (msg.raw or {}).get("threadTs") or msg.ts
            if len(self._threads) > 500:
                for k in list(self._threads)[:100]:
                    self._threads.pop(k, None)
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
        async with c.stream("GET", media["url"], headers=self._bot_headers()) as r:
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
            raise RuntimeError(f"Slack app is not connected (state: {self._status.get('state')})")
        channel = (target or "").strip()
        if not channel:
            raise RuntimeError("target channel id is required")
        thread_ts = self._threads.get(reply_to or "") if reply_to else None
        last = "sent"
        async with self.client_factory() as c:
            if text:
                body: dict = {"channel": channel, "text": text}
                if thread_ts:
                    body["thread_ts"] = thread_ts
                r = await c.post(f"{API}/chat.postMessage", json=body,
                                 headers={**self._bot_headers(), "Content-Type": "application/json; charset=utf-8"})
                last = self._result_ts(r)
            if attachments:
                # File uploads need the multi-step files.uploadV2 flow; send names as text for now.
                names = ", ".join(a.name or os.path.basename(a.path) for a in attachments)
                r = await c.post(f"{API}/chat.postMessage", headers={**self._bot_headers(), "Content-Type": "application/json; charset=utf-8"},
                                 json={"channel": channel, "text": f"(attachment not sent — file uploads are not supported on Slack yet: {names})",
                                       **({"thread_ts": thread_ts} if thread_ts else {})})
                last = self._result_ts(r)
        return last

    @staticmethod
    def _result_ts(r: httpx.Response) -> str:
        data = r.json() if r.status_code == 200 else {}
        if not data.get("ok"):
            raise RuntimeError(f"Slack API {r.status_code}: {data.get('error') or r.text[:200]}")
        return str(data.get("ts") or "sent")

    def self_ids(self) -> set[str]:
        return {self._bot_id} if self._bot_id else set()
