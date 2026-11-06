"""Conversation compaction — summarize an over-long chat history into a compact
recap so the session doesn't blow past the model's context window.

This is the summarizing counterpart to ``loop._fit_context`` (which only *elides*
old message bodies). ``/compact`` (and the auto-compact at ~80% of the window)
calls this to fold the older turns into one short summary the assistant can keep
using. Like ``verify.py`` it's a plain, tool-less, temperature-0 model call through
the same upstream the chat loop uses — the summarizer can't act, only condense.
"""

from __future__ import annotations

import logging

from gateway import egress

logger = logging.getLogger(__name__)

# Cap what we feed the summarizer so the summary call itself can't overflow the
# window: keep the most-recent slice of the head, note the rest was dropped.
_MAX_SOURCE_CHARS = 24_000

_SYSTEM = (
    "You compress a conversation between a user and an AI assistant into a compact "
    "recap so the assistant can continue seamlessly without the full transcript. "
    "Preserve: facts and figures, decisions made, code and file paths, names/IDs, "
    "the user's goals, and any open threads or unfinished steps. Drop pleasantries "
    "and redundancy. Write it as terse notes (not a narrative), in the third person "
    "('The user…', 'The assistant…'). Do not add anything not in the conversation."
)


def _flatten(messages: list[dict]) -> str:
    """Render a message list to plain text for the summarizer."""
    lines: list[str] = []
    for m in messages:
        role = (m.get("role") or "user").upper()
        content = m.get("content")
        if isinstance(content, list):  # multimodal parts → keep the text bits
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict) and p.get("text"))
        text = (content or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


async def summarize_messages(model: str, messages: list[dict],
                             provider: dict | None = None) -> str:
    """Return a compact recap of ``messages`` (the older 'head' of a chat)."""
    source = _flatten(messages)
    if not source.strip():
        return ""
    if len(source) > _MAX_SOURCE_CHARS:  # keep the most recent portion
        source = ("…[older turns omitted]…\n\n" + source[-_MAX_SOURCE_CHARS:])
    user = ("Summarize this conversation so far into a compact recap:\n\n"
            f"{source}\n\n---\nCompact recap:")
    payload = {
        "model": model, "temperature": 0.0,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": user}],
    }
    text = ""
    async for chunk in egress.stream(payload, provider):
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
    return text.strip()
