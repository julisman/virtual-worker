"""Server-Sent Events helpers for the protocol adapters."""

from __future__ import annotations

import json


def sse_event(data: dict, event: str | None = None) -> bytes:
    """Encode one SSE message. Anthropic & Responses use named ``event:`` lines;
    Gemini uses bare ``data:`` only (pass ``event=None``)."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n".encode("utf-8")
