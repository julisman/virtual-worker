"""ChannelManager — the shared spine above every chat transport.

One instance runs for the app's lifetime (started/stopped from ``app.py`` like
the scheduler). It owns one adapter per enabled account and handles every
inbound message the same way regardless of transport:

    inbound → dedup → policy (dm/group/allow-list/pairing) → resolve agent
            → per-conversation FIFO + debounce → slash commands
            → build history from the chat session → run the agent (timeout,
              typing indicator) → strip reasoning → persist both turns
            → chunk + send the reply → delivery audit + activity entry

Everything a transport could do differently (media, typing, groups, caps) is
declared by the adapter's ``capabilities``; the manager adapts.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from datetime import datetime, timezone

from channels import base
from channels.base import Attachment, InboundMessage, registry
from storage import activity_store, agent_store, channel_store, chat_channel_store, chat_store

logger = logging.getLogger(__name__)

#: The inbound message driving the current run (so tools like ``channel_send``
#: can default to "reply here"). None outside a channel turn.
CURRENT_INBOUND: contextvars.ContextVar[InboundMessage | None] = contextvars.ContextVar(
    "channel_current_inbound", default=None)

_CHANNEL_DIRECTIVE = (
    "You are replying inside a chat app (not a web UI). Keep answers concise and "
    "conversational; prefer short paragraphs or simple bullet lists; avoid tables "
    "and heavy markdown. Never include hidden reasoning, system details, tokens, "
    "or credentials in a reply. If you need to show a long result, summarize it "
    "and offer details on request."
)

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_STOPPED_RE = re.compile(r"\n*_\(stopped: reached max tool iterations\)_\s*$")

ACK_TEXT = "Working on it…"
RUN_ERROR_TEXT = "Sorry — something went wrong while answering. Please try again."
NOT_ALLOWED_TEXT = "This number isn't approved to use this assistant."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_reasoning(text: str) -> str:
    """Remove any thinking blocks and loop footers before an external send (FR-C7)."""
    text = _THINK_RE.sub("", text or "")
    text = _STOPPED_RE.sub("", text)
    return _strip_meta_notes(text.strip())


_META_NOTE_RE = re.compile(r"^\s*\[[^\]]{0,400}\][ \t]*\n?")
_META_NOTE_WORDS = re.compile(r"tool|this (reply|turn)", re.IGNORECASE)


def _strip_meta_notes(text: str) -> str:
    """Drop leading bracketed provenance notes the model imitated from the
    ``load_history`` annotation (e.g. "[this reply was produced by actually
    CALLING tools: …]"). Those notes are hidden context for the model, never
    content the user should see. Only brackets that talk about tools/turns are
    dropped, so a legitimate "[link] …" opening survives."""
    out = text or ""
    while True:
        m = _META_NOTE_RE.match(out)
        if not m or not _META_NOTE_WORDS.search(m.group(0)):
            break
        out = out[m.end():]
    return out.strip()


def load_history(session_id: str, turns: int = 20) -> list[dict]:
    """Recent prior ``{role, content}`` turns of a channel conversation (capped).

    Assistant turns that ran tools are annotated with the tool names (from the
    message meta). A multi-tool reply's text is the stitched narration between
    tool calls — replayed verbatim it reads as "narrate and the results appear",
    which teaches the model to fabricate instead of calling tools. The marker
    makes the provenance explicit."""
    from collections import Counter

    sess = chat_store.get_session(session_id)
    if not sess:
        return []
    out = []
    for m in sess.get("messages", []):
        if m.get("role") not in ("user", "assistant") or not (m.get("content") or "").strip():
            continue
        content = m["content"]
        tools = ((m.get("meta") or {}).get("toolsUsed")) or []
        if m["role"] == "assistant" and tools:
            counts = Counter(tools)
            summary = ", ".join(n + (f" ×{k}" if k > 1 else "") for n, k in counts.items())
            content = (f"[this reply was produced by actually CALLING tools: {summary} — "
                       f"every detail came from those tool results. Facts already stated "
                       f"here may be reused without re-calling tools, but NEVER invent new "
                       f"tool results — anything not stated here must come from a fresh "
                       f"tool call. This note is hidden metadata, not part of the visible "
                       f"reply — never write one like it yourself.]\n{content}")
        out.append({"role": m["role"], "content": content})
    return out[-max(0, turns):]


class ChannelManager:
    def __init__(self) -> None:
        self._adapters: dict[str, base.ChannelAdapter] = {}
        self._status: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}       # per-conversation FIFO
        self._pending: dict[str, list[InboundMessage]] = {}  # debounce buffers
        self._tasks: set[asyncio.Task] = set()
        self._started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start adapters for every enabled account. Sync entry (like scheduler)."""
        self._started = True
        try:
            chat_channel_store.prune_events(days=7)
        except Exception:  # noqa: BLE001
            logger.debug("chat_events prune failed", exc_info=True)
        for acct in chat_channel_store.list_accounts():
            if acct["enabled"]:
                self._spawn(self.start_account(acct["id"]))

    async def stop(self) -> None:
        self._started = False
        for aid in list(self._adapters):
            await self.stop_account(aid)
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.get_event_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _data_dir(self, account_id: str) -> str:
        import config
        p = config.APP_HOME / "channels" / account_id
        config.ensure_dir(p)
        return str(p)

    async def start_account(self, account_id: str) -> dict:
        acct = chat_channel_store.get_account(account_id)
        if not acct:
            return {"state": "missing"}
        await self.stop_account(account_id)
        cls = registry.get(acct["kind"])
        if cls is None:
            self._status[account_id] = {"state": "error", "error": f"unknown channel kind {acct['kind']!r}"}
            return self._status[account_id]
        adapter = cls(acct, chat_channel_store.get_secrets(account_id),
                      on_inbound=self.dispatch_inbound, on_status=self._on_status,
                      data_dir=self._data_dir(account_id))
        self._adapters[account_id] = adapter
        try:
            await adapter.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel %s start failed: %s", account_id, exc)
            adapter._set_status("error", error=str(exc)[:300])
        return adapter.status()

    async def stop_account(self, account_id: str) -> None:
        adapter = self._adapters.pop(account_id, None)
        if adapter:
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                logger.debug("channel %s stop failed", account_id, exc_info=True)
        self._status[account_id] = {"state": "off"}

    async def reload_account(self, account_id: str) -> dict:
        acct = chat_channel_store.get_account(account_id)
        if acct and acct["enabled"] and self._started:
            return await self.start_account(account_id)
        await self.stop_account(account_id)
        return self._status.get(account_id, {"state": "off"})

    def adapter(self, account_id: str) -> base.ChannelAdapter | None:
        return self._adapters.get(account_id)

    def status(self, account_id: str) -> dict:
        a = self._adapters.get(account_id)
        if a:
            return a.status()
        return self._status.get(account_id, {"state": "off"})

    def _on_status(self, account_id: str, status: dict) -> None:
        self._status[account_id] = status

    # ------------------------------------------------------------------ #
    # inbound pipeline
    # ------------------------------------------------------------------ #
    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Process one message to completion (policy, debounce, turn, reply).
        Never raises. Adapters use :meth:`dispatch_inbound` instead so their
        socket reader is never blocked for the length of a model run."""
        try:
            await self._handle(msg)
        except Exception:  # noqa: BLE001
            logger.warning("channel inbound handling failed", exc_info=True)

    async def dispatch_inbound(self, msg: InboundMessage) -> None:
        """Schedule :meth:`handle_inbound` on its own task and return at once.
        Per-conversation order is still kept by the debounce buffer + lock."""
        self._spawn(self.handle_inbound(msg))

    async def _handle(self, msg: InboundMessage) -> None:
        if msg.from_me:
            return
        if not msg.has_content():
            return
        acct = chat_channel_store.get_account(msg.account_id)
        if not acct or not acct["enabled"]:
            return
        if chat_channel_store.seen_before(msg.account_id, msg.event_id, msg.conversation_id):
            return
        cfg = acct["config"]
        verdict = self.check_policy(acct, msg)
        if verdict == "pairing":
            req, created = chat_channel_store.request_pairing(
                msg.account_id, msg.sender_id, msg.sender_name)
            if created:
                await self._send(msg.account_id, msg.conversation_id,
                                 f"Hi! This assistant isn't available to your number yet. "
                                 f"Ask the owner to approve pairing code {req['code']}.",
                                 reply_to=msg.event_id, source="pairing")
            return
        if verdict == "deny":
            if msg.kind == "dm" and cfg["dmPolicy"] == "allowlist":
                await self._send(msg.account_id, msg.conversation_id, NOT_ALLOWED_TEXT,
                                 reply_to=msg.event_id, source="policy")
            return
        if verdict != "allow":
            return
        # Media is fetched only now, after policy: observed group chatter never
        # costs bandwidth or disk.
        await msg.load_attachments()
        if not (msg.text or msg.attachments):
            return
        # Debounce: collect rapid consecutive messages from the same conversation.
        key = msg.session_key
        buf = self._pending.setdefault(key, [])
        buf.append(msg)
        if len(buf) > 1:
            return  # the first message's task will drain the buffer
        delay = (cfg.get("debounceMs") or 0) / 1000.0
        if delay > 0:
            await asyncio.sleep(delay)
        batch = self._pending.pop(key, [])
        if not batch:
            return
        merged = self._merge(batch)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # per-conversation FIFO
            await self._turn(acct, merged)

    @staticmethod
    def _merge(batch: list[InboundMessage]) -> InboundMessage:
        if len(batch) == 1:
            return batch[0]
        first = batch[0]
        texts = [m.text for m in batch if m.text]
        atts: list[Attachment] = []
        for m in batch:
            atts.extend(m.attachments)
        first.text = "\n".join(texts)
        first.attachments = atts
        first.mentioned = any(m.mentioned for m in batch)
        first.event_id = batch[-1].event_id
        return first

    def check_policy(self, acct: dict, msg: InboundMessage) -> str:
        """Return ``allow`` | ``deny`` | ``pairing`` | ``ignore`` (FR-C5)."""
        cfg = acct["config"]
        allow = set(cfg.get("allowFrom") or [])
        sender_ok = msg.sender_id in allow
        if msg.kind == "group":
            gp = cfg.get("groupPolicy")
            if gp == "off":
                return "ignore"
            if gp == "mention" and not msg.mentioned:
                return "ignore"
            if gp == "allowlist" and not (sender_ok or msg.conversation_id in allow):
                return "ignore"
            return "allow"
        dp = cfg.get("dmPolicy")
        if dp == "open" or sender_ok:
            return "allow"
        if dp == "pairing":
            return "pairing"
        return "deny"

    def resolve_agent(self, acct: dict, msg: InboundMessage) -> dict | None:
        b = chat_channel_store.get_binding(acct["id"], msg.conversation_id)
        if b:
            a = agent_store.get_agent(b["agentId"])
            if a:
                return a
        if acct.get("defaultAgentId"):
            a = agent_store.get_agent(acct["defaultAgentId"])
            if a:
                return a
        return agent_store.get_default_agent()

    # ------------------------------------------------------------------ #
    # one turn
    # ------------------------------------------------------------------ #
    async def _turn(self, acct: dict, msg: InboundMessage, *, agent_override: dict | None = None,
                    session_override: str | None = None) -> None:
        cfg = acct["config"]
        session_id = session_override or msg.session_key
        tools_used: list[str] = []
        cmd = (msg.text or "").strip().lower()
        if cmd in ("/reset", "/new"):
            chat_store.delete_session(session_id)
            await self._send(acct["id"], msg.conversation_id, "Conversation reset.",
                             reply_to=msg.event_id, source="command")
            return
        agent = agent_override or self.resolve_agent(acct, msg)
        if cmd == "/status":
            name = (agent or {}).get("name") or "assistant"
            model = (agent or {}).get("model") or "active local model"
            await self._send(acct["id"], msg.conversation_id,
                             f"{name} · model: {model} · session: {msg.conversation_id}",
                             reply_to=msg.event_id, source="command")
            return
        if cmd == "/help":
            await self._send(acct["id"], msg.conversation_id,
                             "Commands: /reset (start over), /status, /help. "
                             "Otherwise just write to me.", reply_to=msg.event_id, source="command")
            return
        if agent is None:
            await self._send(acct["id"], msg.conversation_id,
                             "No agent is bound to this chat yet.", reply_to=msg.event_id,
                             source="policy")
            return

        adapter = self._adapters.get(acct["id"])
        user_text = self._user_text(msg)
        title = msg.sender_name or msg.conversation_id
        history = self._history(session_id, cfg.get("historyTurns") or 20)
        messages = history + [self._wire_user(msg, user_text, cfg)]

        token = CURRENT_INBOUND.set(msg)
        ack_task = None
        typing_task = None
        if adapter:
            try:
                await adapter.mark_read(msg)
            except Exception:  # noqa: BLE001
                pass
        try:
            if adapter and adapter.capabilities.get("typing"):
                typing_task = self._spawn(self._typing_loop(adapter, msg.conversation_id))
            ack_after = (cfg.get("ackAfterMs") or 0) / 1000.0
            if ack_after > 0:
                ack_task = self._spawn(self._ack_later(acct["id"], msg, ack_after))
            timeout = float(cfg.get("runTimeoutS") or 0) or None
            try:
                res = await asyncio.wait_for(
                    execute_turn(agent, messages, session_id), timeout=timeout)
                text, err = res[0], res[1]
                tools_used = list(res[2]) if len(res) > 2 else []   # stubs may return a pair
            except asyncio.TimeoutError:
                text, err = "", "run timed out"
        finally:
            CURRENT_INBOUND.reset(token)
            for t in (ack_task, typing_task):
                if t:
                    t.cancel()
            if adapter and adapter.capabilities.get("typing"):
                try:
                    await adapter.typing(msg.conversation_id, False)
                except Exception:  # noqa: BLE001
                    pass

        reply = strip_reasoning(text)
        if err and not reply:
            logger.warning("channel turn error (%s): %s", session_id, err)
            reply = RUN_ERROR_TEXT
        elif not reply:
            reply = "(no reply)"

        # Persist both turns so the agent's Conversations view shows the thread.
        try:
            chat_store.append_message(session_id, "user", user_text, title=title,
                                      agent_id=agent.get("id"),
                                      meta={"channel": acct["kind"], "sender": msg.sender_id,
                                            "senderName": msg.sender_name, "eventId": msg.event_id})
            a_meta: dict = {"channel": acct["kind"]}
            if err:
                a_meta["error"] = err
            if tools_used:
                a_meta["toolsUsed"] = tools_used   # load_history marks tool-backed replies
            chat_store.append_message(session_id, "assistant", reply, title=title,
                                      agent_id=agent.get("id"), meta=a_meta)
        except Exception:  # noqa: BLE001
            logger.debug("channel session persist failed", exc_info=True)

        await self._send(acct["id"], msg.conversation_id, reply, reply_to=msg.event_id,
                         agent_id=agent.get("id"), source="reply")
        try:
            activity_store.log(agent.get("id"), "note",
                               f"Replied on {acct['kind']} to {msg.sender_name or msg.sender_id}",
                               detail={"sessionId": session_id, "conversationId": msg.conversation_id,
                                       "chars": len(reply), "error": err})
        except Exception:  # noqa: BLE001
            pass

    async def _typing_loop(self, adapter: base.ChannelAdapter, target: str) -> None:
        try:
            while True:
                await adapter.typing(target, True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def _ack_later(self, account_id: str, msg: InboundMessage, after: float) -> None:
        try:
            await asyncio.sleep(after)
            await self._send(account_id, msg.conversation_id, ACK_TEXT,
                             reply_to=msg.event_id, source="ack")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _user_text(msg: InboundMessage) -> str:
        text = (msg.text or "").strip()
        parts = []
        if msg.kind == "group" and msg.sender_name:
            parts.append(f"[{msg.sender_name}] {text}" if text else f"[{msg.sender_name}]")
        elif text:
            parts.append(text)
        for a in msg.attachments:
            desc = f"(attached {a.kind}: {a.name or os.path.basename(a.path)} at {a.path} — open it with document_read"
            if a.caption:
                desc += f" — caption: {a.caption}"
            parts.append(desc + ")")
        return "\n".join(parts).strip()

    @staticmethod
    def _wire_user(msg: InboundMessage, text: str, cfg: dict) -> dict:
        images = [a for a in msg.attachments if a.kind == "image"] if cfg.get("sendMediaToModel") else []
        if not images:
            return {"role": "user", "content": text}
        import base64
        import mimetypes
        parts: list[dict] = [{"type": "text", "text": text}]
        for a in images[:4]:
            try:
                raw = open(a.path, "rb").read()
            except OSError:
                continue
            mime = a.mime or mimetypes.guess_type(a.path)[0] or "image/jpeg"
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}})
        return {"role": "user", "content": parts}

    @staticmethod
    def _history(session_id: str, turns: int) -> list[dict]:
        return load_history(session_id, turns)

    # ------------------------------------------------------------------ #
    # outbound
    # ------------------------------------------------------------------ #
    async def _send(self, account_id: str, target: str, text: str, *,
                    reply_to: str | None = None, agent_id: str | None = None,
                    source: str = "reply", attachments: list[Attachment] | None = None,
                    schedule_id: str | None = None) -> dict:
        """Send via the account's adapter, chunked, and record one audit row."""
        adapter = self._adapters.get(account_id)
        acct = chat_channel_store.get_account(account_id)
        kind = (acct or {}).get("kind") or "chat"
        log = dict(channel=f"chat:{kind}", target=target, source=source, agent_id=agent_id,
                   schedule_id=schedule_id, payload_text=text)
        if adapter is None:
            return channel_store.log_delivery(status="failed", error="channel account is not running", **log)
        try:
            chunks = base.split_text(text, adapter.max_text_len) or [""]
            last = ""
            for i, chunk in enumerate(chunks):
                last = await adapter.send(target, chunk, reply_to=reply_to if i == 0 else None,
                                          attachments=attachments if i == len(chunks) - 1 else None)
            rec = channel_store.log_delivery(status="sent", **log)
            rec["messageId"] = last          # platform id of the last chunk (not persisted)
            return rec
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel send via %s failed: %s", kind, exc)
            return channel_store.log_delivery(status="failed", error=str(exc)[:500], **log)

    async def send(self, account_id: str, target: str, text: str, *,
                   agent_id: str | None = None, source: str = "tool",
                   reply_to: str | None = None, schedule_id: str | None = None) -> dict:
        """Public proactive send (tools, deliveries, tests) with the self-send guard."""
        adapter = self._adapters.get(account_id)
        acct = chat_channel_store.get_account(account_id)
        kind = (acct or {}).get("kind") or "chat"
        if adapter and target in adapter.self_ids():
            return channel_store.log_delivery(channel=f"chat:{kind}", target=target, source=source,
                                              status="dropped", agent_id=agent_id, schedule_id=schedule_id,
                                              error="refused: target is the account itself",
                                              payload_text=text)
        return await self._send(account_id, target, text, reply_to=reply_to,
                                agent_id=agent_id, source=source, schedule_id=schedule_id)


async def execute_turn(agent: dict, messages: list[dict], session_id: str) -> tuple[str, str | None, list[str]]:
    """Run a prepared conversation (history + the new user turn) through the
    shared runner with the chat-app directive. Returns ``(text, error,
    tools_used)``. Module-level so tests can monkeypatch it (no model needed);
    a two-tuple from a stub is accepted."""
    from agent import runner

    text_parts: list[str] = []
    last_narration = ""
    err: str | None = None
    tools_used: list[str] = []
    async for ev in runner.stream_run(agent, messages, session_id=session_id,
                                      extra_system=_CHANNEL_DIRECTIVE):
        t = ev.get("type")
        if t == "text":
            text_parts.append(ev.get("delta", ""))
        elif t == "tool_call":
            name = ev.get("name")
            if name and name not in tools_used:
                tools_used.append(str(name))
            # Text streamed before a tool call is narration, not the answer —
            # keep only what follows the last tool call (stash as a fallback).
            if "".join(text_parts).strip():
                last_narration = "".join(text_parts)
            text_parts = []
        elif t == "error":
            err = ev.get("message") or "run failed"
    final = "".join(text_parts).strip() or last_narration.strip()
    return final, err, tools_used


manager = ChannelManager()
