"""Prefix-volatility lint (PRD-17 FR-10) — detector only, never mutates.

The persona system prompt is the cached prompt prefix: if it contains content
that changes between runs (dates, times, UUIDs, hashes), llama.cpp's KV-cache
breaks right there and every turn re-prefills the persona *and* the whole
conversation history. Virtual Worker learned this the hard way (the 2026-07-18 fix that
pulled the clock out of the persona — the clock now lives only in the schedule
tools' results); Headroom's CacheAligner converged on the same invariant.

This module only *finds and reports* volatile-looking content so the UI can
warn the operator — mutating the prefix automatically would itself be a cache
bug. Detection is structural where possible (``datetime.fromisoformat``,
``uuid.UUID``) rather than regex-guesswork.
"""

from __future__ import annotations

import re
import uuid as _uuid
from datetime import datetime

# Candidate patterns are *wide*; each hit is then verified structurally where
# a stdlib parser exists. Kinds mirror what an operator can actually fix.
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s?(?:am|pm|AM|PM|WIB|UTC|GMT)?\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{16,64}\b")
_PHRASE_RE = re.compile(r"current (?:date|time)|today is|as of (?:now|today)|right now it is", re.IGNORECASE)


def _valid_date(s: str) -> bool:
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
        return True
    except ValueError:
        return False


def _valid_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except ValueError:
        return False


def find_volatile(text: str, limit: int = 8) -> list[dict]:
    """Scan ``text`` for content that will differ between runs. Returns up to
    ``limit`` findings as ``{kind, sample, hint}`` dicts (empty list = stable).
    """
    if not text:
        return []
    findings: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, sample: str, hint: str):
        key = f"{kind}:{sample}"
        if key not in seen and len(findings) < limit:
            seen.add(key)
            findings.append({"kind": kind, "sample": sample, "hint": hint})

    for m in _DATE_RE.finditer(text):
        if _valid_date(m.group(0)):
            add("date", m.group(0),
                "a literal date in the persona changes over time and breaks the "
                "prompt cache — the runtime already injects 'now' with each turn")
    for m in _UUID_RE.finditer(text):
        if _valid_uuid(m.group(0)):
            add("uuid", m.group(0),
                "a UUID in the persona is usually a pasted run/session artifact — "
                "move it to a memory note or the task prompt")
    for m in _HEX_RE.finditer(text):
        s = m.group(0)
        # a long hex run with digit+letter mix (hashes), not plain words/numbers
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            add("hash", s[:20] + ("…" if len(s) > 20 else ""),
                "looks like a content hash / token — volatile artifacts don't "
                "belong in the cached persona")
    for m in _PHRASE_RE.finditer(text):
        add("time-phrase", m.group(0),
            "phrases like this usually precede a pasted timestamp; the runtime "
            "injects the current date/time with each turn automatically")
    # Bare times only matter alongside a time-ish phrase or date (avoid flagging
    # schedules like "standup at 09:30" which are *stable* content).
    if any(f["kind"] in ("date", "time-phrase") for f in findings):
        for m in _TIME_RE.finditer(text):
            add("time", m.group(0).strip(),
                "a literal clock time in the persona changes every run")
    return findings
