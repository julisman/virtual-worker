"""The chat-channel plugin contract.

An adapter implements :class:`ChannelAdapter` for one *account* (a connected
WhatsApp number, a Telegram bot token, …). The manager instantiates one adapter
per enabled account, calls ``start()``, and receives normalized
:class:`InboundMessage` objects through the ``on_inbound`` callback. Replies go
back through ``send()``.

Design rules (PRD §8.6):

  • No inbound listener is required for a default transport — adapters dial out.
  • Credentials are write-only: adapters read them from ``secrets`` and never
    echo them in ``status()`` or logs.
  • Adapters never run the agent; they only move messages. The manager owns
    policy, routing, sessions, and the run.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid
from pathlib import Path
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    """A file the sender attached, already saved under the agent-visible media dir."""
    path: str
    mime: str = ""
    name: str = ""
    kind: str = "file"          # image | document | audio | video | file
    caption: str = ""


@dataclass
class InboundMessage:
    """One normalized inbound chat message (FR-C2)."""
    channel: str                # adapter kind: whatsapp | whatsapp_cloud | telegram | …
    account_id: str             # our account row id
    conversation_id: str        # chat/group id on the platform (reply target)
    kind: str                   # "dm" | "group"
    sender_id: str              # platform id of the human who wrote it
    sender_name: str = ""
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: str | None = None   # platform message id this one quotes, if any
    event_id: str = ""            # platform message id — dedup key
    ts: str = ""                  # ISO timestamp
    mentioned: bool = False       # the bot was @mentioned (groups)
    from_me: bool = False         # sent by the connected account itself
    raw: dict = field(default_factory=dict)
    # Adapters that must *fetch* media set this instead of downloading up front;
    # the manager awaits it only once the message has passed policy, so a busy
    # group the bot merely observes never fills the media directory.
    attachment_loader: Callable[[], Awaitable[list["Attachment"]]] | None = field(
        default=None, repr=False, compare=False)

    def has_content(self) -> bool:
        return bool(self.text or self.attachments or self.attachment_loader)

    async def load_attachments(self) -> None:
        """Run the deferred loader (once) and append what it returns."""
        loader, self.attachment_loader = self.attachment_loader, None
        if loader is None:
            return
        try:
            self.attachments.extend(await loader())
        except Exception:  # noqa: BLE001
            logger.warning("attachment download failed for %s", self.event_id, exc_info=True)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.account_id}:{self.conversation_id}"


OnInbound = Callable[[InboundMessage], Awaitable[None]]
OnStatus = Callable[[str, dict], None]


class ChannelAdapter:
    """Base class every transport implements. Methods are async and must never
    raise into the manager for routine transport errors — return a status or
    log instead. ``send`` may raise; the manager records the failure."""

    kind: str = "base"
    #: Human label + setup hints for the UI catalog.
    label: str = "Base"
    description: str = ""
    #: Non-secret config keys (rendered as inputs) and secret keys (write-only).
    config_fields: list[dict] = []
    secret_fields: list[dict] = []
    #: What this transport can do; the manager adapts behavior to it.
    capabilities: dict = {
        "media": False, "typing": False, "groups": False, "threads": False,
        "pairing": False, "webhook": False, "markdown": False,
    }
    #: Max characters per outbound message before the manager splits it.
    max_text_len: int = 4000

    def __init__(self, account: dict, secrets: dict, *, on_inbound: OnInbound,
                 on_status: OnStatus | None = None, data_dir: str = ""):
        self.account = account
        self.secrets = secrets or {}
        self.on_inbound = on_inbound
        self._on_status = on_status
        self.data_dir = data_dir
        self._status: dict = {"state": "off", "detail": "", "me": None, "error": None}

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def status(self) -> dict:
        """Masked, UI-safe status. Never includes secrets."""
        return dict(self._status)

    def _set_status(self, state: str, **extra) -> None:
        self._status.update({"state": state, **extra})
        if self._on_status:
            try:
                self._on_status(self.account.get("id", ""), self.status())
            except Exception:  # noqa: BLE001
                logger.debug("status callback failed", exc_info=True)

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        """Send ``text`` (and optional files) to ``target``; return a platform
        message id (or a short description)."""
        raise NotImplementedError  # pragma: no cover - interface

    async def typing(self, target: str, on: bool = True) -> None:
        """Best-effort typing indicator; default no-op."""
        return None

    async def mark_read(self, msg: InboundMessage) -> None:
        """Best-effort read receipt; default no-op."""
        return None

    # ---- optional surfaces -------------------------------------------------- #
    async def pair(self) -> dict:
        """Begin pairing (QR / code). Returns the current pairing payload."""
        return {"supported": False}

    async def logout(self) -> None:
        """Forget the linked session so the next start requires re-pairing."""
        return None

    def self_ids(self) -> set[str]:
        """Platform ids that mean 'this account' — used by the self-send guard."""
        me = self._status.get("me")
        return {me} if me else set()


class AdapterRegistry:
    """kind → adapter class. Adapters register themselves at import time."""

    def __init__(self) -> None:
        self._kinds: dict[str, type[ChannelAdapter]] = {}

    def register(self, cls: type[ChannelAdapter]) -> type[ChannelAdapter]:
        self._kinds[cls.kind] = cls
        return cls

    def get(self, kind: str) -> type[ChannelAdapter] | None:
        return self._kinds.get(kind)

    def kinds(self) -> list[type[ChannelAdapter]]:
        return list(self._kinds.values())


registry = AdapterRegistry()


def split_text(text: str, limit: int) -> list[str]:
    """Split a long reply on paragraph, then line, then hard boundaries so each
    chunk fits the transport's cap (FR-C9 / WhatsApp ~4096 chars)."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit + 1)
        if cut < limit // 3:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


async def run_with_backoff(name: str, fn: Callable[[], Awaitable[None]],
                           stop: asyncio.Event, *, base: float = 2.0,
                           cap: float = 60.0) -> None:
    """Re-run ``fn`` until ``stop`` is set, with exponential backoff + jitter
    between failures. Shared by adapters that hold a long-lived connection."""
    import random
    delay = base
    while not stop.is_set():
        clean = False
        try:
            await fn()
            delay = base  # a clean return resets the backoff
            clean = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: connection loop error: %s", name, exc)
        if stop.is_set():
            break
        # A clean return means the server asked us to reconnect (Slack
        # ``disconnect``, Discord op 7): open the replacement right away.
        wait = 0.2 if clean else min(cap, delay) * (0.8 + 0.4 * random.random())
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        delay = min(cap, delay * 2)


async def download_to_media_dir(client, url: str, *, data_dir: str, cap: int,
                                name: str = "", mime: str = "", size: int = 0,
                                headers: dict | None = None) -> str | None:
    """Stream ``url`` into ``<data_dir>/media/<random><ext>`` with a byte cap.
    Returns the path, or ``None`` when the file is too large, the fetch fails,
    or the stream overruns the cap (the partial file is removed)."""
    if size and size > cap:
        return None
    media_dir = Path(data_dir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(name or "").suffix or mimetypes.guess_extension(mime or "") or ""
    out = media_dir / f"{uuid.uuid4().hex[:12]}{ext}"
    written = 0
    try:
        async with client.stream("GET", url, headers=headers or {}) as r:
            if r.status_code != 200:
                return None
            with open(out, "wb") as fh:
                async for chunk in r.aiter_bytes():
                    written += len(chunk)
                    if written > cap:
                        fh.close()
                        out.unlink(missing_ok=True)
                        return None
                    fh.write(chunk)
    except Exception:  # noqa: BLE001
        out.unlink(missing_ok=True)
        raise
    return str(out)


def attachment_from_media(path: str, media: dict, caption: str = "") -> "Attachment":
    mime = media.get("mime") or ""
    return Attachment(path=path, mime=mime, name=media.get("name") or os.path.basename(path),
                      kind=media.get("kind") or ("image" if mime.startswith("image/") else "document"),
                      caption=caption)


def _retry_after_seconds(r) -> float | None:
    """Seconds to wait after a 429, from the header or a JSON ``retry_after``."""
    hdr = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-After")
    if hdr:
        try:
            return float(hdr)
        except ValueError:
            pass
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("retry_after") is not None:
            return float(body["retry_after"])
    except Exception:  # noqa: BLE001
        pass
    return 1.0


async def post_with_retry(client, url: str, *, attempts: int = 3, max_wait: float = 10.0, **kw):
    """``client.post`` that honours one or two 429 replies by sleeping for the
    advertised ``retry_after`` (capped) before retrying. Other statuses return."""
    r = None
    for i in range(max(1, attempts)):
        r = await client.post(url, **kw)
        if r.status_code != 429 or i == attempts - 1:
            return r
        wait = min(max_wait, _retry_after_seconds(r) or 1.0)
        logger.info("rate limited by %s; retrying in %.1fs", url, wait)
        await asyncio.sleep(wait)
    return r


class WebSocketAdapter(ChannelAdapter):
    """Shared lifecycle for adapters that hold one long-lived WebSocket
    (Discord Gateway, Slack Socket Mode): a stop event, the supervising task,
    the current socket, and a ``ws_connect`` test seam."""

    #: Test seam: async callable(url) -> websocket-like (recv/send/close); None = websockets.connect
    ws_connect = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ws = None

    def _run(self, name: str, session) -> None:
        self._stop.clear()
        self._task = asyncio.get_event_loop().create_task(run_with_backoff(name, session, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        ws, self._ws = self._ws, None
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


def close_code(exc: BaseException) -> tuple[int | None, str]:
    """``(code, reason)`` from a websockets ``ConnectionClosed`` (any version)."""
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        return getattr(rcvd, "code", None), getattr(rcvd, "reason", "") or ""
    return getattr(exc, "code", None), getattr(exc, "reason", "") or ""
