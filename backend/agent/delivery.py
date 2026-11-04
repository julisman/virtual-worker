"""Outbound delivery — give a virtual worker a mouth (PRD-01).

Today an agent or scheduled run can finish real work, but its output only lands
in Activity where it sits until a human looks. This module adds a single,
auditable way to *push* a result to where the operator is: a Virtual Worker chat thread, an
OS notification, or an opt-in webhook.

Design invariants (from the PRD):

  • **Explicit target, resolved at send time.** The destination is always named
    in the call or the schedule config — never a captured/last-seen recipient.
    This hard-codes the lesson from the sibling engine's PR #648, where a cron
    job froze the current chat's sender and leaked owner digests to customers.
  • **Local-first.** ``in_app`` + ``desktop`` work fully offline and need no
    config. Network egress (``webhook``) is opt-in: a URL must be in an enabled
    webhook channel's allow-list before anything is POSTed to it.
  • **Every attempt is audited.** Sent / failed / dropped — all written to the
    ``deliveries`` table for the UI and as the safety record.

Adding a channel = adding a connector here + listing it in ``CHANNELS``; mirrors
how tools are pluggable. ``make_deliver_tool`` exposes ``deliver`` to the agent
loop, mirroring ``make_schedule_tools``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import httpx

from storage import channel_store

logger = logging.getLogger(__name__)

# Channels shipped in this milestone. Local ones are always available; network
# ones are opt-in (see webhook).
CHANNELS = ("in_app", "desktop", "webhook", "chat")
# Channels that address a real recipient — an explicit target is mandatory
# (FR-3 / anti-PR-#648). Desktop is a local notification with no recipient.
REQUIRES_TARGET = {"in_app", "webhook", "chat"}
NETWORK_CHANNELS = {"webhook", "chat"}

MAX_PAYLOAD_BYTES = 256 * 1024   # rate/size guard (FR-14)
WEBHOOK_TIMEOUT = 15.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Connectors
# --------------------------------------------------------------------------- #

def _send_in_app(target: str, text: str, subject: str | None,
                 agent_id: str | None, source: str) -> str:
    """Post the message into a chat session (``target`` = session id / alias)."""
    from storage import chat_store
    body = f"**{subject}**\n\n{text}" if subject else text
    meta = {"delivered": True, "channel": "in_app", "source": source,
            "agentId": agent_id, "subject": subject}
    chat_store.append_message(target, "assistant", body, meta=meta,
                              title=subject or "Delivered", agent_id=agent_id)
    return f"posted to chat session {target!r}"


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_desktop(target: str, text: str, subject: str | None) -> str:
    """Raise a macOS Notification Center notification (best-effort, local)."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        raise RuntimeError("desktop notifications are only available on macOS")
    title = _osascript_escape((subject or "Virtual Worker")[:120])
    body = _osascript_escape(text.replace("\n", " ")[:480])
    script = f'display notification "{body}" with title "{title}"'
    if target:
        script += f' subtitle "{_osascript_escape(target[:120])}"'
    subprocess.run(["osascript", "-e", script], timeout=10, check=True,
                   capture_output=True)
    return "notification posted"


async def _send_webhook(target: str, text: str, subject: str | None,
                        data, agent_id: str | None, source: str) -> str:
    """POST JSON to an allow-listed webhook URL (opt-in network egress)."""
    # Lazy import keeps the SSRF guard reuse without a heavy import at module load.
    from agent.tools import _is_private_host

    url = target.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if url not in channel_store.webhook_allowlist():
        raise RuntimeError(
            "webhook URL is not in any enabled webhook channel allow-list — "
            "enable it first (network egress is opt-in)")
    if _is_private_host(url):
        raise RuntimeError("refused: webhook host is private/loopback")
    payload = {"channel": "webhook", "target": url, "text": text,
               "subject": subject, "data": data, "agent": agent_id,
               "source": source, "ts": _now_iso()}
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as c:
        r = await c.post(url, json=payload)
        r.raise_for_status()
    return f"POST {r.status_code}"


async def _send_chat(target: str, text: str, subject: str | None,
                     agent_id: str | None, source: str) -> dict:
    """Send through a connected chat channel. ``target`` is
    ``<accountId>:<conversation>`` (a phone number or group id); the channel
    manager audits the send itself, so its record is returned as-is."""
    from channels.manager import manager
    if ":" not in target:
        raise RuntimeError("chat target must be '<accountId>:<phone-or-chat-id>'")
    account_id, conv = target.split(":", 1)
    body = f"*{subject}*\n\n{text}" if subject else text
    return await manager.send(account_id.strip(), conv.strip(), body, agent_id=agent_id, source=source)


# --------------------------------------------------------------------------- #
# Central deliver — the single auditable send path
# --------------------------------------------------------------------------- #

async def deliver(channel: str, target: str, text: str, *,
                  subject: str | None = None, data=None,
                  agent_id: str | None = None, source: str = "tool",
                  schedule_id: str | None = None) -> dict:
    """Send ``text`` to one ``channel``/``target`` and record the attempt.

    Returns the persisted delivery record (always — even on failure, so the
    caller and the audit log agree). Never raises; connector errors become a
    ``failed`` record. ``target`` is taken verbatim from the caller and is never
    substituted with a remembered recipient (FR-3)."""
    channel = (channel or "").strip().lower()
    target = (target or "").strip()
    text = text or ""

    def _record(status: str, error: str = "") -> dict:
        return channel_store.log_delivery(
            channel=channel, target=target, source=source, status=status,
            agent_id=agent_id, schedule_id=schedule_id, error=error,
            payload_text=text)

    if channel not in CHANNELS:
        return _record("failed", f"unknown channel {channel!r}; known: {', '.join(CHANNELS)}")
    if channel in REQUIRES_TARGET and not target:
        # FR-3: explicit target only — never infer/capture a recipient.
        return _record("failed", "an explicit 'target' is required — recipients are never inferred")

    nbytes = len(text.encode("utf-8"))
    if nbytes > MAX_PAYLOAD_BYTES:
        return _record("dropped", f"payload {nbytes} bytes exceeds {MAX_PAYLOAD_BYTES}-byte cap")

    try:
        if channel == "in_app":
            _send_in_app(target, text, subject, agent_id, source)
        elif channel == "desktop":
            _send_desktop(target, text, subject)
        elif channel == "webhook":
            await _send_webhook(target, text, subject, data, agent_id, source)
        elif channel == "chat":
            return await _send_chat(target, text, subject, agent_id, source)
        return _record("sent")
    except Exception as exc:  # noqa: BLE001
        logger.warning("delivery via %s failed: %s", channel, exc)
        return _record("failed", str(exc))


# --------------------------------------------------------------------------- #
# Agent-facing tool (mirrors make_schedule_tools)
# --------------------------------------------------------------------------- #

def make_deliver_tool(agent_id: str) -> dict[str, dict]:
    """``{name: {schema, fn}}`` for the ``deliver`` tool, bound to ``agent_id``."""

    async def deliver_fn(channel: str = "", target: str = "", text: str = "",
                         subject: str = "", data=None) -> str:
        channel = (channel or "").strip()
        target = (target or "").strip()
        text = text or ""
        if not channel:
            return "deliver needs 'channel' — one of: in_app, desktop, webhook, chat."
        if not text.strip():
            return "deliver needs 'text' — the message to send."
        if channel.lower() in REQUIRES_TARGET and not target:
            return ("deliver needs an explicit 'target' (a chat session id/alias for "
                    "in_app, or a webhook URL). Recipients are never inferred.")
        rec = await deliver(channel, target, text, subject=(subject or None),
                            data=data, agent_id=agent_id, source="tool")
        if rec["status"] == "sent":
            where = f" to {target}" if target else ""
            return f"delivered via {rec['channel']}{where} (delivery id={rec['id']})"
        return f"delivery via {channel} {rec['status']}: {rec['error']}"

    return {
        "deliver": {
            "fn": deliver_fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": "deliver",
                    "description": (
                        "Send a message to a person/destination NOW via a configured channel: "
                        "'in_app' (post into a chat session — target is the session id/alias), "
                        "'desktop' (a macOS notification — no target needed), or 'webhook' "
                        "(HTTP POST to an allow-listed URL — target is that URL). You MUST name "
                        "an explicit 'target'; the system never guesses or reuses a recipient. "
                        "Use this to report a result or escalate mid-run."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel": {"type": "string", "description": "in_app | desktop | webhook | chat"},
                            "target": {"type": "string", "description": "explicit destination: chat session id/alias, a webhook URL, or for chat '<accountId>:<phone-or-chat-id>'"},
                            "text": {"type": "string", "description": "the message body"},
                            "subject": {"type": "string", "description": "optional title/subject"},
                        },
                        "required": ["channel", "text"],
                    },
                },
            },
        },
    }
