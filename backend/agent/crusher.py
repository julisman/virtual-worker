"""Content-aware tool-result compression (PRD-17: Crusher + CCR).

Replaces blind prefix-truncation (``loop._cap``) with structure-preserving
compression at the single point where a tool result enters the conversation:

- **JSON** (any shape — top-level arrays, ``{"data": {...}}`` envelopes, big
  single objects) is reduced recursively: arrays of similar objects become
  stats + representative rows (first/last always; error-looking rows and
  numeric extremes always survive), long strings become head+tail slices,
  small scalars pass through verbatim.
- **Line-based text** (logs, listings, search results) gets consecutive-dup
  collapsing, then head + tail + error lines within the cap.
- **Everything else** falls back to head+tail — strictly better than the old
  head-only cut.

When bytes are dropped, the compressed text ends with a sentinel::

    <<vw:a1b2c3 · 214 of 230 rows omitted — call retrieve_full('a1b2c3')>>

and the caller stores the original under that hash in ``storage.ccr_store``
so the model can recover it on demand via the ``retrieve_full`` tool
(``make_retrieve_tool``). Content at or below the cap passes through
byte-identical.

Design invariants (see PRD-17):
- **Deterministic**: same input ⇒ same output bytes (no wall-clock, no
  randomness, stable ordering) — a resumed/retried run must reproduce the
  prompt exactly or the KV-cache prefix breaks.
- **Live-zone only**: callers apply this once, at insertion; history is never
  rewritten.
- **Never worse than the status quo**: any internal failure falls back to the
  old truncation behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Sentinel + retrieval -------------------------------------------------------

RETRIEVE_TOOL_NAME = "retrieve_full"

# Per-run budget on retrieve_full calls — stops a model from re-inflating the
# context by retrieving everything it sees a sentinel for.
RETRIEVE_BUDGET_PER_RUN = 5
# A retrieved original is windowed to this many chars per call (page with
# ``offset``). Never re-crushed — that would mint hash-of-hash chains.
RETRIEVE_WINDOW_CHARS = 8000

# Router thresholds ----------------------------------------------------------

_MIN_ROWS_TO_CRUSH = 4          # arrays smaller than this aren't worth stats
_MIN_LINES_FOR_LINE_MODE = 20   # below this, head+tail does fine
_SMALL_VALUE_CHARS = 240        # dict values at/below this pass verbatim
_ROW_HEAD, _ROW_TAIL = 3, 2     # rows always kept from each end of an array

# Error detection — a row/line that looks like a failure must survive
# compression (it is disproportionately likely to be what the model needs).
_ERROR_KEYS = ("status", "level", "severity", "state", "result", "error",
               "errors", "ok", "success", "retcode", "code")
_ERROR_TOKENS = ("error", "fail", "failed", "failure", "exception", "fatal",
                 "critical", "denied", "timeout", "timed out", "refused",
                 "invalid", "warn", "warning")
_ERROR_LINE_RE = re.compile(
    r"\b(error|fail(ed|ure)?|exception|fatal|critical|denied|timeout|traceback|warn(ing)?)\b",
    re.IGNORECASE)


@dataclass
class CrushResult:
    """What the loop splices into the conversation in place of the raw result.

    ``hash`` is set iff bytes were dropped — the caller must then store the
    original under it (``ccr_store.put``) so ``retrieve_full`` can find it.
    """
    text: str
    strategy: str          # passthrough | json | lines | headtail
    saved_chars: int       # len(original) - len(text); 0 = untouched
    hash: str | None = None


def content_hash(text: str) -> str:
    """Deterministic short id for a result (sha256 hex, first 16 — 64 bits;
    8 was collision-prone enough within a busy 24h TTL window to matter)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# ─── public entry ───────────────────────────────────────────────────────────


def crush(text: str, tool_name: str, cap: int) -> CrushResult:
    """Compress ``text`` toward ``cap`` chars, structure-aware. Never raises:
    any internal failure degrades to the legacy head-only truncation."""
    if text is None:
        return CrushResult(text="", strategy="passthrough", saved_chars=0)
    if len(text) <= cap:
        return CrushResult(text=text, strategy="passthrough", saved_chars=0)
    h = content_hash(text)
    try:
        parsed = _try_json(text)
        if parsed is not None:
            body, omitted = _crush_json(parsed, cap, tool_name)
            strategy = "json"
        elif text.count("\n") >= _MIN_LINES_FOR_LINE_MODE:
            body, omitted = _crush_lines(text, cap)
            strategy = "lines"
        else:
            body, omitted = _head_tail(text, cap), "chars"
            strategy = "headtail"
        sentinel = _sentinel(h, omitted if isinstance(omitted, str) else omitted)
        out = f"{body}\n{sentinel}"
        # Hard guarantee: never blow materially past the cap even if the
        # structure resisted reduction (e.g. one giant string field).
        if len(out) > cap + len(sentinel) + 200:
            out = f"{_head_tail(body, cap)}\n{sentinel}"
        return CrushResult(text=out, strategy=strategy,
                           saved_chars=len(text) - len(out), hash=h)
    except Exception:
        logger.exception("crush failed for %s; falling back to plain truncation", tool_name)
        legacy = text[:cap] + f"\n…[{len(text) - cap} chars truncated to fit the model context]"
        return CrushResult(text=legacy, strategy="headtail",
                           saved_chars=len(text) - len(legacy), hash=None)


def _sentinel(h: str, omitted) -> str:
    what = omitted if isinstance(omitted, str) else f"{omitted} chars omitted"
    return (f"<<vw:{h} · {what} — if you need a specific omitted row or value, "
            f"call {RETRIEVE_TOOL_NAME}('{h}', query='<text to find>')>>")


# ─── JSON reducer ───────────────────────────────────────────────────────────


def _try_json(text: str):
    s = text.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except (ValueError, RecursionError):
        return None


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _crush_json(value, cap: int, tool_name: str) -> tuple[str, str]:
    """Reduce a parsed JSON value to ~cap chars. Returns (body, omitted-desc)."""
    stats = {"rows_omitted": 0, "rows_total": 0, "chars_cut": 0}
    reduced = _reduce(value, max(cap - 200, 400), stats)
    body = _dumps(reduced)
    if stats["rows_total"]:
        omitted = f"{stats['rows_omitted']} of {stats['rows_total']} rows omitted"
    else:
        omitted = "long values elided"
    return body, omitted


def _reduce(value, budget: int, stats: dict):
    """Recursively shrink ``value`` to roughly ``budget`` rendered chars."""
    s = _dumps(value)
    if len(s) <= budget:
        return value

    if isinstance(value, list):
        if len(value) >= _MIN_ROWS_TO_CRUSH and all(isinstance(x, dict) for x in value):
            return _crush_array(value, budget, stats)
        # mixed / scalar list: keep from the front until the budget is spent
        kept, used = [], 2
        for x in value:
            xs = _dumps(x)
            if used + len(xs) > budget and kept:
                break
            kept.append(x)
            used += len(xs) + 1
        dropped = len(value) - len(kept)
        if dropped > 0:
            stats["rows_total"] += len(value)
            stats["rows_omitted"] += dropped
            kept.append(f"…[{dropped} more items in full result]")
        return kept

    if isinstance(value, dict):
        return _reduce_dict(value, budget, stats)

    if isinstance(value, str):
        return _slice_string(value, budget, stats)

    return value  # small scalars never reach here (len(s) <= budget)


def _reduce_dict(d: dict, budget: int, stats: dict) -> dict:
    """Keep every key (structure is cheap and informative); spend the budget
    on values proportionally to their size, so one giant field can't starve
    the rest."""
    sizes = {k: len(_dumps(v)) for k, v in d.items()}
    total = sum(sizes.values()) or 1
    small = {k for k, n in sizes.items() if n <= _SMALL_VALUE_CHARS}
    spent_small = sum(sizes[k] for k in small)
    remaining = max(budget - spent_small - 2 * len(d), 200)
    big_total = sum(sizes[k] for k in sizes if k not in small) or 1
    out = {}
    for k, v in d.items():  # original key order — deterministic
        if k in small:
            out[k] = v
        else:
            share = max(int(remaining * sizes[k] / big_total), 120)
            out[k] = _reduce(v, share, stats)
    return out


def _slice_string(s: str, budget: int, stats: dict) -> str:
    if len(s) <= budget:
        return s
    head = max(int(budget * 0.75), 80)
    tail = max(int(budget * 0.15), 40)
    cut = len(s) - head - tail
    stats["chars_cut"] += cut
    return f"{s[:head]} …[{cut} chars elided]… {s[-tail:]}"


def _crush_array(rows: list, budget: int, stats: dict) -> dict:
    """Array of similar objects → summary + representative rows.

    Kept rows: first/last anchors, every error-looking row, and the min/max
    holder of each numeric field. Constants are factored out once. The whole
    thing renders as one self-describing object so the model can trust what
    it is looking at.
    """
    n = len(rows)
    stats["rows_total"] += n

    # Field survey (bounded: sample up to 200 rows for stats)
    sample = rows if n <= 200 else rows[:100] + rows[-100:]
    field_vals: dict[str, list] = {}
    for r in sample:
        for k, v in r.items():
            field_vals.setdefault(k, []).append(v)

    constants, field_summary = {}, {}
    for k, vals in field_vals.items():
        try:
            uniq = set(_dumps(v) for v in vals)
        except TypeError:
            uniq = set()
        if len(uniq) == 1 and len(vals) == len(sample):
            constants[k] = vals[0]
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            field_summary[k] = {"type": "number", "min": min(vals), "max": max(vals)}
        else:
            field_summary[k] = {"type": "mixed", "distinct": len(uniq)}

    if n > len(sample):
        # The survey sampled (n > 200) — verify its "constant" claims over ALL
        # rows: a value sitting in the un-sampled middle must not contradict
        # the summary the model is told to trust. Demoted fields get exact
        # stats (numeric) or an approximate distinct count (mixed).
        for k in list(constants):
            try:
                ref = _dumps(constants[k])
                if any(_dumps(r.get(k)) != ref for r in rows):
                    constants.pop(k)
                    vals = [r.get(k) for r in rows]
                    if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in vals):
                        field_summary[k] = {"type": "number",
                                            "min": min(vals), "max": max(vals)}
                    else:
                        field_summary[k] = {"type": "mixed",
                                            "distinct": len({_dumps(v) for v in vals})}
            except TypeError:
                pass

    # Row selection (dedup by index, order preserved)
    keep_idx: list[int] = list(range(min(_ROW_HEAD, n)))
    keep_idx += [i for i in range(max(n - _ROW_TAIL, 0), n) if i not in keep_idx]
    for i, r in enumerate(rows):
        if i not in keep_idx and _looks_error_row(r):
            keep_idx.append(i)
    for k, meta in field_summary.items():
        if meta.get("type") == "number":
            vals = [(r.get(k), i) for i, r in enumerate(rows)
                    if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
            if vals:
                (vmin, imin), (vmax, imax) = min(vals), max(vals)
                # The note tells the model these are exact over ALL rows — make
                # that true even when the type survey sampled (n > 200): an
                # extreme sitting in the un-sampled middle must show here.
                meta["min"], meta["max"] = vmin, vmax
                for i in (imin, imax):
                    if i not in keep_idx:
                        keep_idx.append(i)
    keep_idx = sorted(set(keep_idx))

    # Render kept rows within a per-row share of the budget
    overhead = len(_dumps(constants)) + len(_dumps(field_summary)) + 200
    per_row = max((budget - overhead) // max(len(keep_idx), 1), 150)
    kept = []
    for i in keep_idx:
        row = {k: v for k, v in rows[i].items() if k not in constants}
        kept.append({"_i": i, **_reduce_dict(row, per_row, stats)}
                    if len(_dumps(row)) > per_row else {"_i": i, **row})

    stats["rows_omitted"] += n - len(keep_idx)
    return {
        "_vw_compressed": {
            "totalRows": n,
            "keptRows": len(keep_idx),
            "keptIndexes": keep_idx if len(keep_idx) <= 40 else None,
            "constants": constants or None,
            "fields": field_summary or None,
            "note": ("TRUST THIS SUMMARY: totalRows and fields.*.min/max are exact, "
                     "computed over ALL rows. rows[] includes: head/tail anchors, "
                     "EVERY error/failed-looking row, and the min/max holder row of "
                     "each numeric field ('_i' = original index). Only retrieve the "
                     "original for details of a specific omitted row."),
        },
        "rows": kept,
    }


def _looks_error_row(row: dict) -> bool:
    for k in _ERROR_KEYS:
        v = row.get(k)
        if v is None:
            continue
        if k == "retcode" and isinstance(v, int):
            if v != 0:
                return True
            continue
        if isinstance(v, bool):
            if (k in ("ok", "success")) and not v:
                return True
            continue
        if isinstance(v, str) and any(t in v.lower() for t in _ERROR_TOKENS):
            return True
    return False


# ─── line compactor ─────────────────────────────────────────────────────────


def _crush_lines(text: str, cap: int) -> tuple[str, str]:
    lines = text.split("\n")
    total = len(lines)

    # 1) collapse consecutive duplicates
    collapsed: list[str] = []
    run_start = 0
    for i in range(1, total + 1):
        if i == total or lines[i] != lines[run_start]:
            count = i - run_start
            collapsed.append(lines[run_start] if count == 1
                             else f"{lines[run_start]}  ×{count}")
            run_start = i
    if sum(len(l) + 1 for l in collapsed) <= cap:
        return "\n".join(collapsed), f"{total} lines deduplicated"

    # 2) head + tail + error lines, budgeted
    head_budget, tail_budget, err_budget = int(cap * 0.5), int(cap * 0.25), int(cap * 0.2)
    head, used = [], 0
    for l in collapsed:
        if used + len(l) + 1 > head_budget:
            break
        head.append(l)
        used += len(l) + 1
    tail, used = [], 0
    for l in reversed(collapsed[len(head):]):
        if used + len(l) + 1 > tail_budget:
            break
        tail.append(l)
        used += len(l) + 1
    tail.reverse()
    middle = collapsed[len(head):len(collapsed) - len(tail)]
    errs, used = [], 0
    for l in middle:
        if _ERROR_LINE_RE.search(l):
            if used + len(l) + 1 > err_budget:
                break
            errs.append(l)
            used += len(l) + 1
    skipped = len(middle) - len(errs)
    parts = head
    if errs:
        parts = parts + [f"…[matched {len(errs)} error/warn lines from the elided middle]…"] + errs
    if skipped > 0:
        parts = parts + [f"…[{skipped} lines elided]…"]
    parts = parts + tail
    return "\n".join(parts), f"{skipped} of {total} lines omitted"


# ─── head+tail fallback ─────────────────────────────────────────────────────


def _head_tail(text: str, cap: int) -> str:
    head = int(cap * 0.72)
    tail = int(cap * 0.18)
    cut = len(text) - head - tail
    if cut <= 0:
        return text
    return f"{text[:head]}\n…[{cut} chars elided]…\n{text[-tail:]}"


# ─── the retrieve_full self-tool ────────────────────────────────────────────


def make_retrieve_tool() -> dict[str, dict]:
    """Registry entry for ``retrieve_full`` — pull a compressed result's
    original from the CCR store. One instance per run (the closure carries the
    per-run retrieve budget)."""
    from storage import ccr_store  # late import: keeps crusher importable standalone

    calls = {"n": 0}

    async def retrieve_full(hash: str = "", offset: int = 0, query: str = "") -> str:
        h = (hash or "").strip().lower()
        # 8–16 hex: current sentinels carry 16, but markers minted before the
        # hash was lengthened (still live in old conversations) carry 8.
        if not re.fullmatch(r"[0-9a-f]{8,16}", h):
            return ("retrieve_full needs the hex hash from a <<vw:HASH …>> "
                    "marker that appears in this conversation.")
        if calls["n"] >= RETRIEVE_BUDGET_PER_RUN:
            return (f"retrieve budget reached ({RETRIEVE_BUDGET_PER_RUN} per run) — "
                    "answer from what you already have.")
        calls["n"] += 1
        # Sync SQLite off the event loop (put already goes via to_thread).
        row = await asyncio.to_thread(ccr_store.get, h)
        if row is None:
            return (f"no stored original for '{h}' (expired or unknown) — "
                    "answer from the compressed view.")
        content = row["content"]
        if query:
            # Targeted lookup: jump straight to the match instead of paging
            # blind. Case-insensitive; returns the window around the first hit.
            q = query.lower()
            hits = []
            start = 0
            low = content.lower()
            while len(hits) < 50:
                i = low.find(q, start)
                if i < 0:
                    break
                hits.append(i)
                start = i + max(len(q), 1)
            if not hits:
                return (f"'{query}' not found in the original ({len(content)} chars). "
                        "Try a different query, or page with offset.")
            half = RETRIEVE_WINDOW_CHARS // 2
            at = hits[0]
            lo = max(at - half // 4, 0)
            window = content[lo:lo + half]
            more = f" ({len(hits)} matches total)" if len(hits) > 1 else ""
            return (f"[match at char {at}{more}; context follows]\n{window}")
        off = max(int(offset or 0), 0)
        window = content[off:off + RETRIEVE_WINDOW_CHARS]
        remaining = len(content) - off - len(window)
        note = ""
        if off or remaining > 0:
            note = (f"\n[chars {off}–{off + len(window)} of {len(content)}"
                    + (f"; {remaining} more — call retrieve_full('{h}', offset={off + len(window)})"
                       if remaining > 0 else "") + "]")
        return window + note

    schema = {
        "type": "function",
        "function": {
            "name": RETRIEVE_TOOL_NAME,
            "description": (
                "Look up the original, uncompressed content of a tool result that "
                "was compressed to save context (marked <<vw:HASH · … omitted>>). "
                "Pass the 16-hex hash from a <<vw:...>> marker appearing in this "
                "conversation. If no <<vw:...>> marker appears in the conversation, "
                "there is nothing to retrieve — do not call this tool. "
                "Prefer `query` to jump straight to the row/value you need — it "
                "returns the text around the first match. Only page with `offset` "
                "when you truly need to read the whole original. Do NOT call this "
                "for totals, min/max or error rows — the compressed summary already "
                "has those exactly."),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {"type": "string",
                             "description": "the hex hash from a <<vw:...>> marker in this conversation"},
                    "query": {"type": "string",
                              "description": "text to find in the original (e.g. an id like 'ORD-203'); returns the surrounding context"},
                    "offset": {"type": "integer",
                               "description": "char offset for sequential paging (default 0); ignored when query is set"},
                },
                "required": ["hash"],
            },
        },
    }
    return {RETRIEVE_TOOL_NAME: {"schema": schema, "fn": retrieve_full}}
