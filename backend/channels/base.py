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
        try:
            await fn()
            delay = base  # a clean return resets the backoff
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: connection loop error: %s", name, exc)
        if stop.is_set():
            break
        wait = min(cap, delay) * (0.8 + 0.4 * random.random())
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        delay = min(cap, delay * 2)
