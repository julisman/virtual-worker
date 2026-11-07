"""The agentic loop: stream assistant turns, run tool calls, feed results back.

Uses the same upstream as the rest of the gateway (the local model). Streams
typed events so the UI can show tool activity inline:

  {"type": "text", "delta": "..."}
  {"type": "tool_call", "id", "name", "args"}
  {"type": "tool_result", "id", "name", "result"}
  {"type": "done"} | {"type": "error", "message"}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

import config
from gateway import egress, providers as provider_catalog
from gateway.upstream import UpstreamError
from storage import ccr_store, debug_db
from . import crusher
from . import run_registry
from . import tools as toolkit
from .summarize import summarize_messages

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6          # fallback when a caller passes max_iterations=None
UNLIMITED_BACKSTOP = 10_000  # "unlimited" (max_iterations=0) still terminates eventually

# PRD-17 kill switch: VW_COMPRESSION=0 restores blind truncation everywhere,
# regardless of per-agent settings.
COMPRESSION_ENABLED = os.environ.get("VW_COMPRESSION", "1").lower() not in ("0", "false")

# --- Context management ---------------------------------------------------- #
# A long tool loop can pile up huge tool outputs (a 200k-char read_file, web
# pages, shell logs) and overflow the model's context window. Two bounds keep
# the model-facing conversation inside budget without losing the trail (the full
# results still go to the Debug store + the UI):
#   1. cap each tool result that's fed *back into the model*, and
#   2. trim the whole conversation to a char budget before each model call,
#      eliding the oldest bulky turns while protecting the system prompt, the
#      original task, and the most recent turns.
# Budget is in characters (~3.5 chars/token). Smaller budgets mean less re-prefill
# per turn → noticeably faster on local models (the main cost in a tool loop is
# re-reading the growing context). Defaults are moderate; an agent can opt into
# ``tightContext`` (guardrail) for the tighter budgets below when speed matters
# more than carrying lots of context (e.g. MCP-heavy chats with big tool outputs).
MAX_TOOL_RESULT_CHARS = int(os.environ.get("VW_MAX_TOOL_RESULT_CHARS", "6000"))
# Repetition penalty for the LOCAL upstream only (small fine-tunes can lock into
# a phrase cycle — "di-fine-tune di OpenAI, di-fine-tune di Google, …" — because
# nothing in temperature/top_p/top_k punishes a repeat). Sent as BOTH spellings:
# mlx_lm.server reads `repetition_penalty`, llama.cpp's server reads
# `repeat_penalty`; each ignores the other's key. Remote providers reject unknown
# params, so this never goes out with a provider target. 0 disables.
REPEAT_PENALTY = float(os.environ.get("VW_REPEAT_PENALTY", "1.1"))
# Explicit output budget on every request. Without it the SERVER default rules,
# and mlx_lm.server's is small — thinking models (<think> spends output tokens
# before the visible answer) hit it constantly, surfacing as "stopped: hit the
# model's output token limit" mid-answer. 3072 leaves room for a long think +
# a full reply on local models; remote providers cap it themselves.
MAX_OUTPUT_TOKENS = int(os.environ.get("VW_MAX_OUTPUT_TOKENS", "3072"))
# On a remote reasoning model the output budget covers thinking + answer in one
# pool: 3072 (sized for local models) starves a medium+/high-effort model on a
# heavy context — it burns the whole budget reasoning and the reply is empty
# (finish "length" with no text). Give those turns a real floor. An explicit
# VW_MAX_OUTPUT_TOKENS pins the budget for everything, reasoning included.
REASONING_MIN_OUTPUT_TOKENS = 16384
_OUTPUT_BUDGET_PINNED = "VW_MAX_OUTPUT_TOKENS" in os.environ
CONTEXT_CHAR_BUDGET = int(os.environ.get("VW_CONTEXT_CHARS", "40000"))
# Remote providers (ChatGPT/GLM/…) ship far larger context windows than the local
# model the 40k default is sized for. When a run targets a provider and the agent
# left ``contextChars`` on auto, use this instead so the loop stops eliding loaded
# skills / tool results / the user's own alert text on models that can hold them.
REMOTE_CONTEXT_CHARS = int(os.environ.get("VW_REMOTE_CONTEXT_CHARS", "240000"))
TIGHT_TOOL_RESULT_CHARS = int(os.environ.get("VW_TIGHT_TOOL_RESULT_CHARS", "3000"))
TIGHT_CONTEXT_BUDGET = int(os.environ.get("VW_TIGHT_CONTEXT_CHARS", "20000"))
# MCP tool results (pis_get_*, confluence_*, …) are often large structured JSON the
# model genuinely needs in full, so the small general cap above would lose the rows
# past the limit. Exempt MCP tools with a much larger bound; an agent can override
# it per-run via the ``mcpToolResultChars`` guardrail.
MCP_TOOL_RESULT_CHARS = int(os.environ.get("VW_MCP_TOOL_RESULT_CHARS", "40000"))
# A ``wiki_read`` returns a full wiki page, which the general 6000 cap would clip.
# Give it its own, larger default cap so a page reaches the model intact; an agent
# can override it per-run via the ``wikiReadChars`` guardrail (PRD-13).
WIKI_READ_RESULT_CHARS = int(os.environ.get("VW_WIKI_READ_CHARS", "8000"))
KEEP_RECENT_MSGS = 6

# --- Lazy tool exposure ---------------------------------------------------- #
# Tool *schemas* (name + description + full JSON parameter spec) are sent on
# EVERY model call so the model knows what it can call — a flat per-turn tax paid
# whether or not a tool is used. MCP servers make this brutal: 4 servers can inject
# 90+ tools / 40k+ tokens, so a bare "hi" prefills 40k tokens (~2 min on a local
# model) before the first token. Fix: tools flagged ``deferred`` (all MCP tools —
# see mcp_client.build_registry) are hidden from the model and replaced by a single
# ``search_tools`` meta-tool. The model calls it with keywords; matching tools are
# "unlocked" (their real schemas join the next turn's tools) and become callable.
# Baseline prefill drops to the core tools (~a few k tokens) regardless of how many
# MCP servers are attached. Only kicks in when the hidden schemas are actually big
# (below the threshold, deferring just adds a needless round-trip), so small setups
# are unchanged.
SEARCH_TOOLS_NAME = "search_tools"
LAZY_TOOL_THRESHOLD_CHARS = int(os.environ.get("VW_LAZY_TOOL_CHARS", "6000"))

# Unlocks persist per chat session: each run() is one user turn, so without this
# every follow-up turn would re-hide the tools the model just unlocked and force
# another search_tools round-trip (or worse — a hallucinated direct call copied
# from earlier conversation text). Only the unlocked *names* are remembered; the
# agent's registry is still rebuilt per run, so tools detached between turns
# simply stop matching and drop out.
_SESSION_UNLOCKS: dict[str, set[str]] = {}
_SESSION_UNLOCKS_MAX = 1000

# Follow-through guard: a turn that ANNOUNCES an action ("saya panggil dulu MCP…",
# "let me check…") but makes no tool call would otherwise end the run — the user
# gets a promise that nothing will ever fulfil. Small local models do this a lot.
# One nudged retry tells the model to actually make the call (or answer plainly).
# Bilingual (id/en), conservative patterns; one shot per run.
_ANNOUNCE_RE = re.compile(
    r"(sedang (?:di)?proses|saya (?:panggil|cek|ambil|jalankan|coba)|akan saya |"
    r"tunggu sebentar|sebentar ya|"
    r"\blet me (?:call|check|fetch|run|look|pull|grab)|"
    r"\bi(?:'ll| will) (?:call|check|fetch|run|get|look|pull)\b|"
    r"\bone (?:moment|sec)\b|\bchecking now\b|\bfetching now\b)", re.I)


def _search_tools_schema(n_deferred: int, servers: list[str]) -> dict:
    srv = ", ".join(servers) if servers else "external services"
    return {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_NAME,
            "description": (
                f"Discover and unlock additional tools. {n_deferred} more tools are "
                f"available (from: {srv}) but their definitions are hidden to keep your "
                "context small. Call this with keywords for the action you need "
                "(e.g. 'create jira issue', 'search confluence', 'send email') and the "
                "matching tools become directly callable on your next step. If the first "
                "results aren't right, call it again with different keywords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Keywords describing the tool or action you need."},
                    "limit": {"type": "integer",
                              "description": "Max tools to unlock (default 12, max 25)."},
                },
                "required": ["query"],
            },
        },
    }


def _error_shaped(name: str, result: str) -> bool:
    """True when a tool result is one of the error shapes the tool layer emits.
    Anchored at the start (and keyed on the tool name) so a normal result that
    merely mentions "error" isn't misread: MCP isError results ("[tool error] …",
    mcp_client:59), and dispatch failures — "<tool> error: …", "bad arguments for
    <tool> …", and "unknown tool: …"."""
    return (result.startswith("[tool error]")
            or result.startswith(f"{name} error:")
            or result.startswith(f"bad arguments for {name}")
            or result.startswith("unknown tool:"))


def _make_search_tools(registry: dict[str, dict], deferred: set[str], active: set[str],
                       remembered: set[str] | None = None):
    """Build the ``search_tools`` fn — a closure that unlocks deferred tools by
    adding their names to the shared ``active`` set the loop reads each turn.
    ``remembered`` is the session's persistent unlock set (see _SESSION_UNLOCKS);
    unlocks are mirrored into it so they survive into the next run."""

    def _sig(name: str) -> str:
        fn = registry[name]["schema"]["function"]
        desc = " ".join((fn.get("description") or "").split())
        if len(desc) > 200:
            desc = desc[:197] + "…"
        params = (fn.get("parameters") or {})
        props = list((params.get("properties") or {}).keys())
        req = set(params.get("required") or [])
        shown = props[:10]
        arglist = ", ".join(p + ("*" if p in req else "") for p in shown)
        if len(props) > len(shown):
            arglist += ", …"
        return f"- `{name}`({arglist}) — {desc}"

    async def search_tools(query: str = "", limit: int = 12) -> str:
        try:
            limit = max(1, min(int(limit or 12), 25))
        except (TypeError, ValueError):
            limit = 12
        terms = [t for t in (query or "").lower().split() if t]
        hits: list[tuple[int, str]] = []
        if terms:
            for name in deferred:
                fn = registry[name]["schema"]["function"]
                hay = (name + " " + (fn.get("description") or "")).lower()
                score = sum(1 for t in terms if t in hay)
                if score:
                    hits.append((score, name))
            hits.sort(key=lambda x: (-x[0], x[1]))
        if not hits:
            names = sorted(deferred)
            preview = "\n".join(f"- `{n}`" for n in names[:40])
            more = f"\n…and {len(names) - 40} more." if len(names) > 40 else ""
            return (f"No tool matched {query!r}. {len(names)} tools are available:\n"
                    f"{preview}{more}\n\nCall search_tools again with keywords that "
                    "match one of these names or its purpose.")
        picked = [n for _, n in hits[:limit]]
        for n in picked:
            active.add(n)
            if remembered is not None:
                remembered.add(n)
        lines = "\n".join(_sig(n) for n in picked)
        return ("Unlocked these tools — you can now call them directly (an argument "
                f"marked * is required):\n{lines}")

    return search_tools


def _cap(text: str, limit: int) -> str:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} chars truncated to fit the model context]"


_IMAGE_CONTEXT_CHARS = 4_096


def _content_chars(content) -> int:
    """Estimate model-facing content size without counting encoded image bytes.

    Vision providers tokenize an image from its dimensions/content; they do not
    treat every character of an inline base64 data URL as a text token.  Give
    each image a conservative fixed allowance while still counting all text in
    a multimodal message verbatim.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_content_chars(part) for part in content)
    if isinstance(content, dict):
        part_type = content.get("type")
        if part_type in {"image", "image_url", "input_image"}:
            return _IMAGE_CONTEXT_CHARS
        if part_type in {"text", "input_text"}:
            return len(str(content.get("text") or ""))
        return len(json.dumps(content))
    return len(str(content))


def _msg_chars(m: dict) -> int:
    n = _content_chars(m.get("content"))
    for tc in m.get("tool_calls") or []:
        n += len(json.dumps(tc))
    return n


def _estimate_tokens(m: dict) -> int:
    """Conservative per-message token estimate for the un-measured tail (PRD-23
    FR-3): chars/4. Used only in window mode."""
    return _msg_chars(m) // 4


def _context_tokens(convo: list[dict], meter: dict) -> int:
    """The window-mode meter (FR-3/4). Before any valid usage the whole convo is
    a pure chars/4 estimate; once a response reports real ``prompt_tokens`` the
    covered prefix is exact (``lastPromptTokens + lastCompletionTokens``) and only
    the messages appended after the covered point are estimated."""
    if not meter.get("valid"):
        return sum(_estimate_tokens(m) for m in convo)
    base = int(meter.get("promptTokens", 0)) + int(meter.get("completionTokens", 0))
    tail = sum(_estimate_tokens(m) for m in convo[meter.get("coveredCount", 0):])
    return base + tail


def _fit_context(convo: list[dict], budget: int) -> list[dict]:
    """Return a copy of ``convo`` trimmed to ``budget`` chars. Protects the
    system message, the first user message (the task), and the last
    ``KEEP_RECENT_MSGS`` messages; elides the bodies of older tool/assistant
    turns oldest-first until under budget."""
    total = sum(_msg_chars(m) for m in convo)
    if total <= budget:
        return convo
    n = len(convo)
    protected = {i for i, m in enumerate(convo) if m.get("role") == "system"}
    first_user = next((i for i, m in enumerate(convo) if m.get("role") == "user"), None)
    if first_user is not None:
        protected.add(first_user)
    protected |= set(range(max(0, n - KEEP_RECENT_MSGS), n))
    out = [dict(m) for m in convo]
    for i in range(n):
        if total <= budget:
            break
        if i in protected:
            continue
        m = out[i]
        content = m.get("content")
        if isinstance(content, str) and len(content) > 120:
            saved = len(content) - 100
            m["content"] = content[:100] + "\n…[elided to fit the model context]"
            total -= saved
    if total <= budget:
        return out
    # Second pass: the protected recent window itself can exceed the budget
    # (e.g. one huge tool result). Shrink protected TOOL results — never the
    # system prompt, the task, or the final message — until under budget.
    per_msg = max(budget // (KEEP_RECENT_MSGS + 2), 500)
    for i in range(n):
        if total <= budget:
            break
        m = out[i]
        if m.get("role") != "tool" or i == n - 1:
            continue
        content = m.get("content")
        if isinstance(content, str) and len(content) > per_msg:
            saved = len(content) - per_msg
            m["content"] = content[:per_msg] + "\n…[elided to fit the model context]"
            total -= saved
    return out


# --- Auto-compaction -------------------------------------------------------- #
# ``_fit_context`` above is the always-on backstop: it *elides* old bodies to
# 100-char stubs. Auto-compaction (opt-in, guardrail ``autoCompact``) is the nicer
# alternative — before the conversation reaches ``compactAt``% of the budget it
# folds the older turns (everything but the system prompt, the task, and the recent
# tail) into ONE LLM-written recap so the evidence survives as prose instead of
# being stubbed out. It runs right before ``_fit_context`` (which still runs as the
# guaranteed backstop if the recap is unavailable or the tail alone overflows).
_RECAP_PREFIX = (
    "[Recap of earlier conversation — compacted to fit the context window; "
    "details preserved below are authoritative:]\n")
# The foldable slice must be worth folding — below these it's a no-op (the recap
# call would cost a round-trip to save almost nothing).
_COMPACT_MIN_MSGS = 2
_COMPACT_MIN_CHARS = 2000


def _has_tool_calls(m: dict) -> bool:
    return bool(m.get("tool_calls"))


def _resolve_budget(tight_context: bool, context_chars: int,
                    provider: dict | None) -> tuple[int, int]:
    """Resolve ``(ctx_budget, tool_cap)`` in chars. Precedence:
      1. ``tight_context`` (the explicit "fast" switch) → tight budgets, wins.
      2. explicit ``context_chars > 0`` → use it (re-clamped [8000, 2M] defensively).
      3. auto: a remote ``provider`` target → REMOTE_CONTEXT_CHARS; else the local
         default. The tool-result cap then scales with the resolved budget."""
    if tight_context:
        return TIGHT_CONTEXT_BUDGET, TIGHT_TOOL_RESULT_CHARS
    if context_chars and context_chars > 0:
        ctx_budget = max(8000, min(int(context_chars), 2_000_000))
    elif provider is not None:
        ctx_budget = REMOTE_CONTEXT_CHARS
    else:
        ctx_budget = CONTEXT_CHAR_BUDGET
    tool_cap = max(MAX_TOOL_RESULT_CHARS, min(24000, ctx_budget // 10))
    return ctx_budget, tool_cap


def _keep_budget(mode: str, ctx_budget: int, window: int | None,
                 compact_keep_amount: int) -> int:
    """The size budget (tokens in window mode, chars in chars mode) that the
    ``compactKeep="size"`` verbatim tail may spend (FR-21). Auto (amount 0) scales
    to the window/budget with a floor+cap; a user amount is clamped to ≤ 1/3 so
    the tail can never dominate the window."""
    if mode == "window" and window:
        if compact_keep_amount and compact_keep_amount > 0:
            return min(int(compact_keep_amount), max(window // 3, 1))
        return max(4_000, min(window * 10 // 100, 20_000))
    if compact_keep_amount and compact_keep_amount > 0:
        return min(int(compact_keep_amount), max(ctx_budget // 3, 1))
    return max(16_000, min(ctx_budget * 10 // 100, 80_000))


def _fold_end(convo: list[dict], *, mode: str, ctx_budget: int, window: int | None,
              compact_keep: str, compact_keep_amount: int) -> int:
    """Where the verbatim tail starts (FR-20). ``compact_keep="messages"`` keeps a
    fixed COUNT of recent messages (today's behavior); ``"size"`` walks backward
    from the newest accumulating sizes until the keep budget is spent. Only this
    boundary changes — the pair-safety walk / protections / recap are untouched."""
    n = len(convo)
    if compact_keep == "size":
        budget = _keep_budget(mode, ctx_budget, window, compact_keep_amount)
        size = _estimate_tokens if (mode == "window" and window) else _msg_chars
        acc, i = 0, n - 1
        while i >= 0:
            acc += size(convo[i])
            i -= 1
            if acc > budget:  # this message included, but the budget is now spent
                break
        return i + 1
    keep = int(compact_keep_amount) if compact_keep_amount and compact_keep_amount > 0 else KEEP_RECENT_MSGS
    return n - keep


async def _maybe_compact(
    convo: list[dict],
    *,
    model: str,
    provider: dict | None,
    ctx_budget: int,
    compact_at: int,
    last_compact_len: int,
    mode: str = "chars",
    window: int | None = None,
    effective_reserve: int = 0,
    context_tokens: int | None = None,
    compact_keep: str = "messages",
    compact_keep_amount: int = 0,
) -> tuple[list[dict], dict | None, int]:
    """Fold older turns into one recap when the convo passes its compaction
    trigger. Returns ``(convo, debug_entry_or_None, new_last_compact_len)``.

    Trigger by mode: chars mode fires past ``compact_at``% of ``ctx_budget``;
    window mode (FR-6) fires when ``context_tokens > window − effective_reserve``.
    The verbatim-tail cut is selected by ``compact_keep`` (FR-20/21).

    Compact-once bookkeeping: ``last_compact_len`` is the message count of the
    convo returned by the previous attempt. A new attempt is only made once the
    convo has grown past that (so a success — which shrinks the convo — or a
    fall-back doesn't re-fire every iteration; one retry per new message)."""
    total = sum(_msg_chars(m) for m in convo)
    n = len(convo)
    if mode == "window" and window:
        compacts_at = window - effective_reserve
        ctx_tokens = context_tokens if context_tokens is not None else _context_tokens(convo, {})
        over = ctx_tokens > compacts_at
    else:
        threshold = ctx_budget * compact_at / 100
        compacts_at = None
        ctx_tokens = None
        over = total > threshold
    if not over:
        return convo, None, last_compact_len
    # Compact-once: no growth since the last attempt ⇒ don't retry.
    if last_compact_len >= 0 and n <= last_compact_len:
        return convo, None, last_compact_len

    first_user = next((i for i, m in enumerate(convo) if m.get("role") == "user"), None)
    if first_user is None:
        return convo, None, n
    fold_start = first_user + 1
    fold_end = _fold_end(convo, mode=mode, ctx_budget=ctx_budget, window=window,
                         compact_keep=compact_keep, compact_keep_amount=compact_keep_amount)
    fold_end = max(fold_start, min(fold_end, n))
    # Pair-safety: never split an assistant carrying tool_calls from its tool
    # result(s) — neither by folding the assistant while its results sit in the
    # kept tail, nor by leaving orphan tool results at the head of the tail when
    # the boundary lands mid-group. Move the boundary earlier until the tail
    # starts on a non-tool message and the fold doesn't end on a tool_calls turn.
    while fold_end > fold_start and (
            _has_tool_calls(convo[fold_end - 1])
            or (fold_end < n and convo[fold_end].get("role") == "tool")):
        fold_end -= 1
    fold_slice = convo[fold_start:fold_end]
    fold_chars = sum(_msg_chars(m) for m in fold_slice)
    if len(fold_slice) < _COMPACT_MIN_MSGS or fold_chars < _COMPACT_MIN_CHARS:
        return convo, None, n  # nothing worth folding — _fit_context handles it

    try:
        summary = await summarize_messages(model, fold_slice, provider=provider)
    except Exception:  # noqa: BLE001
        logger.debug("auto-compact: summarizer failed", exc_info=True)
        summary = ""
    if not summary:
        entry = {"type": "compaction", "success": False,
                 "text": (f"[auto-compact] fell back — recap unavailable; "
                          f"{len(fold_slice)} msgs / {total} chars left for _fit_context"),
                 "timestamp": _now_iso()}
        return convo, entry, n
    recap = {"role": "user", "content": _RECAP_PREFIX + summary}
    new_convo = convo[:fold_start] + [recap] + convo[fold_end:]
    after = sum(_msg_chars(m) for m in new_convo)
    entry = {"type": "compaction", "success": True,
             "text": (f"[auto-compact] folded {len(fold_slice)} msgs into a recap — "
                      f"{total} → {after} chars"),
             "timestamp": _now_iso()}
    if mode == "window" and window:
        # FR-19: record the trigger evaluation when window-mode compaction fires.
        # The persisted copy rides in ``usage`` (the store's free JSON column); the
        # flat keys stay for callers that read the returned entry directly.
        entry["mode"] = "window"
        entry["window"] = window
        entry["contextTokens"] = ctx_tokens
        entry["compactsAt"] = compacts_at
        entry["usage"] = {"mode": "window", "window": window,
                          "contextTokens": ctx_tokens, "compactsAt": compacts_at}
    return new_convo, entry, len(new_convo)


def registry_from_names(names: list[str]) -> dict[str, dict]:
    """Build a per-request tool registry {name: {schema, fn}} from built-in
    tool names — the default registry when no agent customises it."""
    return {n: toolkit.TOOLS[n] for n in (names or []) if n in toolkit.TOOLS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shape_usage(usage: dict | None) -> dict:
    """Shape one turn's raw usage into the frontend ``Usage`` dict —
    ``input`` / ``output`` / ``totalTokens`` / ``cacheRead`` / ``cacheWrite``
    (all optional; ``cost`` omitted). Unknown fields are left out rather than
    written as zeros, so an entry only carries what the provider actually
    reported for that turn."""
    u = usage or {}
    out: dict = {}
    pt = int(u.get("prompt_tokens") or 0)
    ct = int(u.get("completion_tokens") or 0)
    if pt:
        out["input"] = pt
    if ct:
        out["output"] = ct
    if pt or ct:
        out["totalTokens"] = pt + ct
    cr = int(u.get("cacheRead") or 0)
    cw = int(u.get("cacheWrite") or 0)
    if cr:
        out["cacheRead"] = cr
    if cw:
        out["cacheWrite"] = cw
    return out


def _assistant_entry(text: str, usage: dict | None, thinking: str | None = None) -> dict:
    """An ``assistant`` Debug-trace entry carrying this turn's shaped usage (when
    the turn reported any) so the UI can attribute tokens/cache reads per turn.
    ``thinking`` is the turn's captured reasoning (provider-compat side channel),
    persisted into debug_entries' existing ``thinking`` column when present."""
    e = {"type": "assistant", "text": text, "timestamp": _now_iso()}
    if usage:
        e["usage"] = usage
    if thinking:
        e["thinking"] = thinking
    return e


def _text_of(m: dict) -> str:
    """Text content of one message — a plain string, or the joined text parts of a
    multimodal/list content."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("text"))
    return ""


def _first_text(messages: list[dict], role: str) -> str:
    for m in messages:
        if m.get("role") == role:
            return _text_of(m)
    return ""


def _last_text(messages: list[dict], role: str) -> str:
    for m in reversed(messages):
        if m.get("role") == role:
            return _text_of(m)
    return ""


async def _dispatch(registry: dict[str, dict], name: str, args: dict) -> str:
    """Run a tool from the per-request registry (built-in, load_skill, or MCP)."""
    tool = registry.get(name)
    if not tool:
        return f"unknown tool: {name}"
    try:
        return await tool["fn"](**(args or {}))
    except TypeError as exc:
        return f"bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool %s failed: %s", name, exc)
        return f"{name} error: {exc}"


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


def _tag_prefix_len(s: str, tag: str) -> int:
    """Longest proper prefix of ``tag`` that ``s`` ends with (0 if none)."""
    for k in range(min(len(tag) - 1, len(s)), 0, -1):
        if s.endswith(tag[:k]):
            return k
    return 0


class _ThinkFilter:
    """Incrementally drop ``<think>…</think>`` spans from streamed deltas.

    Thinking-capable local models (Qwen3-style) emit their reasoning inline in
    ``content`` unless the server splits it out — with the reasoning guardrail
    off, this keeps it away from the user. A possible partial tag at a chunk
    boundary is held back until the next delta resolves it; an unclosed think
    block at end-of-stream is dropped entirely."""

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""
        self._skip_ws = False  # eat the whitespace right after a </think>

    def _emit(self, seg: str, out: list[str]) -> None:
        if self._skip_ws:
            seg = seg.lstrip()
            if seg:
                self._skip_ws = False
        if seg:
            out.append(seg)

    def feed(self, delta: str) -> str:
        self._buf += delta
        out: list[str] = []
        while self._buf:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            i = self._buf.find(tag)
            if i >= 0:
                if not self._in_think:
                    self._emit(self._buf[:i], out)
                self._buf = self._buf[i + len(tag):]
                self._in_think = not self._in_think
                self._skip_ws = not self._in_think
                continue
            # No full tag yet: hold back a possible partial tag, then either
            # emit (literal text) or discard (thinking) the rest.
            hold = _tag_prefix_len(self._buf, tag)
            emit, self._buf = self._buf[:len(self._buf) - hold], self._buf[len(self._buf) - hold:]
            if not self._in_think:
                self._emit(emit, out)
            break
        return "".join(out)

    def flush(self) -> str:
        """End of stream: emit any held-back literal text; drop an open think."""
        out: list[str] = []
        if not self._in_think:
            self._emit(self._buf, out)
        self._buf, self._in_think = "", False
        return "".join(out)


async def _stream_turn(payload: dict, provider: dict | None = None,
                       strip_think: bool = False):
    """Stream one assistant turn. Returns (text, tool_calls, finish_reason) and
    yields ('text', delta) tuples for content as it arrives. ``provider`` is the
    external egress target (or None for the local upstream). ``strip_think``
    removes inline <think> reasoning from both the deltas and the returned text."""
    text = ""
    thinking = ""
    reasoning_details = None
    filt = _ThinkFilter() if strip_think else None
    calls: dict[int, dict] = {}
    finish = None
    usage = None
    provider_items = None
    async for chunk in egress.stream(payload, provider):
        # Gateways can 200 the SSE handshake and then stream an error event
        # ({"error": {...}}). Ignoring it makes the turn look like a silent
        # empty stream — surface it as an upstream error so the run reports
        # the gateway's actual message instead of "(no response)".
        err = chunk.get("error")
        if err and not chunk.get("choices"):
            msg = err.get("message") if isinstance(err, dict) else err
            raise UpstreamError(502, f"provider streamed an error: {str(msg)[:400]}")
        if chunk.get("providerItems") is not None:
            provider_items = chunk["providerItems"]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            # OpenRouter-dialect gateways stream reasoning in a side channel: first
            # non-empty of reasoning_content/reasoning/reasoning_text wins per delta
            # (never double-count the same text). reasoning_details, when present on
            # the delta or a full message, is kept as the last-seen complete list.
            for key in ("reasoning_content", "reasoning", "reasoning_text"):
                if delta.get(key):
                    thinking += delta[key]
                    break
            rd = delta.get("reasoning_details") or (ch.get("message") or {}).get("reasoning_details")
            if rd is not None:
                reasoning_details = rd
            if delta.get("content"):
                piece = filt.feed(delta["content"]) if filt else delta["content"]
                if piece:
                    text += piece
                    yield ("text", piece)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    if filt and (tail := filt.flush()):
        text += tail
        yield ("text", tail)
    ordered = [calls[i] for i in sorted(calls)]
    yield ("end", {"text": text, "tool_calls": ordered, "finish": finish,
                    "usage": usage, "providerItems": provider_items,
                    "thinking": thinking or None, "reasoning_details": reasoning_details})


def _log_activity(agent_id: str, name: str, args: dict, task_id: str | None,
                  trace: dict | None = None) -> None:
    """Mirror a tool call into the agent's activity feed (best-effort).

    task_* tools record their own (richer) activity, so we skip them here to
    avoid duplicates. File writes are tagged ``file_change``; the rest ``tool_call``.
    ``trace`` (``{debugRunId, debugSessionId}``) is merged into ``detail`` so the
    Activity feed can deep-link the row to its exact Debug trace."""
    if not agent_id or name.startswith("task_"):
        return
    try:
        from storage import activity_store
        if name in ("write_file", "edit_file"):
            activity_store.log(agent_id, "file_change", f"{name} {args.get('path', '')}".strip(),
                               task_id=task_id, detail={"tool": name, **(trace or {})})
        else:
            hint = args.get("query") or args.get("command") or args.get("url") or args.get("path") or ""
            summary = f"{name}" + (f": {str(hint)[:80]}" if hint else "")
            activity_store.log(agent_id, "tool_call", summary, task_id=task_id,
                               detail={"tool": name, **(trace or {})})
    except Exception:  # noqa: BLE001
        logger.debug("activity log failed", exc_info=True)


def _is_transient_upstream_error(exc: UpstreamError) -> bool:
    """Return whether a provider error is safe to retry at the turn boundary."""
    status = exc.status_code
    body = (exc.body or "").lower()
    return status in (408, 429) or status >= 500 or any(
        marker in body for marker in ("server_error", "idle timeout", "overloaded")
    )


async def run(
    messages: list[dict],
    model: str,
    tools: list[str] | dict[str, dict] | None = None,
    temperature: float = 0.7,
    session_id: str = "chat:adhoc",
    system: str | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    repeat_penalty: float | None = None,
    reasoning_effort: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    max_iterations: int | None = None,
    wrapup_warn_at: int | None = None,
    max_output_tokens: int | None = None,
    provider: dict | None = None,
    tight_context: bool = False,
    context_chars: int = 0,
    auto_compact: bool = False,
    compact_at: int = 80,
    context_mode: str = "chars",
    context_window: int | None = None,
    context_window_source: str | None = None,
    effective_reserve: int = 0,
    compact_keep: str = "messages",
    compact_keep_amount: int = 0,
    mcp_tool_cap: int | None = None,
    wiki_tool_cap: int | None = None,
    compression: bool = True,
    lazy_tools: bool = True,
    hide_reasoning: bool = False,
) -> AsyncIterator[dict]:
    """Drive the tool loop, yielding typed event dicts. Records the full call
    trail (prompt → tool calls → answer) to the Debug store under session_id.

    ``tools`` is a per-request registry ``{name: {schema, fn}}`` (the agent's
    composed tools); a bare list of built-in tool names is also accepted for
    backwards compatibility. ``system``, if given, is injected as the leading
    system message (replacing any client-sent one). This loop adds no volatile
    per-turn text of its own — the clock agents see is the session-pinned line
    ``compose.clock_line`` puts in ``system`` (stable across a chat burst so
    the prefix cache holds), and the scheduler still reports "now" in its own
    tool results."""
    registry = registry_from_names(tools) if isinstance(tools, list) or tools is None else dict(tools)
    # Capability gate: a remote provider that hasn't opted into OpenAI's reasoning
    # extension (the custom OpenAI-compatible gateway, default-off) must not receive
    # reasoning_effort. resolve_target stamps the capability on the target; missing
    # key ⇒ forward (built-in remotes, unchanged). Drop it once here so the payload
    # builders and the Debug record all agree. A model with a managed thinking
    # dialect keeps its effort regardless — the dialect, not the passthrough
    # toggle, decides how it goes on the wire.
    if (provider is not None and not provider.get("reasoning_effort", True)
            and (provider.get("kind") == "responses"
                 or provider.get("thinking_format") not in ("openrouter", "zai"))):
        reasoning_effort = None

    def _out_budget() -> int:
        """Per-turn output token budget (one pool for thinking + answer).
        Precedence: per-agent setting > VW_MAX_OUTPUT_TOKENS env > auto
        (the reasoning floor for remote medium+ effort, else the default)."""
        if max_output_tokens:
            return max_output_tokens
        if (provider is not None and not _OUTPUT_BUDGET_PINNED
                and reasoning_effort in ("medium", "high", "xhigh")):
            return max(MAX_OUTPUT_TOKENS, REASONING_MIN_OUTPUT_TOKENS)
        return MAX_OUTPUT_TOKENS

    convo = list(messages)
    if system is not None:
        convo = [m for m in convo if m.get("role") != "system"]
        convo.insert(0, {"role": "system", "content": system})

    sidecars: dict[int, dict] = {}
    if (provider or {}).get("stateless_reasoning_replay"):
        try:
            from storage import reasoning_store
            enc_key = (provider or {}).get("reasoning_enc_key")
            sidecars = {row["seq"]: row
                         for row in reasoning_store.get_for_session(session_id, enc_key)}
        except Exception:
            logger.debug("reasoning sidecar rehydrate failed", exc_info=True)

    # Lazy tool exposure: hide big ``deferred`` tool sets (all MCP tools) behind a
    # search_tools meta-tool so the per-turn prompt stays small. ``active`` is the
    # set of tool names whose schemas are sent this turn; search_tools grows it as
    # the model unlocks tools. Only engage when the hidden schemas are actually
    # large — otherwise show everything (no needless discovery round-trip).
    deferred = {n for n, e in registry.items() if e.get("deferred")}
    deferred_chars = sum(len(json.dumps(registry[n]["schema"])) for n in deferred)
    # ``lazy_tools=False`` (guardrail ``lazyTools: "off"``) always exposes every
    # schema directly — the search_tools discovery hop reliably defeats small
    # local models, which announce the action instead of making the extra call.
    if lazy_tools and deferred and deferred_chars >= LAZY_TOOL_THRESHOLD_CHARS and SEARCH_TOOLS_NAME not in registry:
        active = {n for n in registry if n not in deferred}
        # Re-apply this session's earlier unlocks so follow-up turns can call
        # them directly instead of re-discovering them every message.
        remembered = _SESSION_UNLOCKS.setdefault(session_id, set())
        if len(_SESSION_UNLOCKS) > _SESSION_UNLOCKS_MAX:
            for stale in list(_SESSION_UNLOCKS)[:len(_SESSION_UNLOCKS) - _SESSION_UNLOCKS_MAX]:
                if stale != session_id:
                    del _SESSION_UNLOCKS[stale]
        active |= {n for n in remembered if n in deferred}
        hidden = deferred - active
        servers = sorted({p[1] for n in hidden
                          if len(p := n.split("__")) >= 3 and p[0] == "mcp"})
        registry[SEARCH_TOOLS_NAME] = {
            "schema": _search_tools_schema(len(hidden), servers),
            "fn": _make_search_tools(registry, deferred, active, remembered),
        }
        active.add(SEARCH_TOOLS_NAME)
    else:
        active = set(registry)

    def _active_schemas() -> list[dict]:
        # ``retrieve_full`` recovers a compressed result by its <<vw:HASH>>
        # marker — but until a marker actually exists in the conversation there
        # is nothing to retrieve. Hiding its schema until then stops literal
        # models from calling it (with the description's example hash) in a run
        # that never compressed anything. Markers arrive two ways: carried in by
        # prior-turn history, or minted mid-run when a big result is crushed —
        # both land in ``convo``, which this per-turn check re-reads each call.
        has_marker = any("<<vw:" in str(m.get("content") or "") for m in convo)
        return [registry[n]["schema"] for n in registry if n in active
                and (n != crusher.RETRIEVE_TOOL_NAME or has_marker)]

    ctx_budget, tool_cap = _resolve_budget(tight_context, context_chars or 0, provider)
    compact_at = int(compact_at) if compact_at else 80
    _last_compact_len = -1

    # PRD-23 window mode: meter real tokens against the resolved window. Only
    # engaged when a window actually resolved; otherwise fall back to chars mode
    # byte-for-byte (FR-15). The _fit_context backstop then runs on a char budget
    # derived from the window so its mechanics are untouched (FR-8).
    _mode = "window" if (context_mode == "window" and context_window) else "chars"
    _eff_reserve = int(effective_reserve or 0) if _mode == "window" else 0
    if _mode == "window":
        fit_budget = max(8_000, (context_window - _eff_reserve) * 4)
    else:
        fit_budget = ctx_budget
    # The window-mode meter state (FR-3/4), invalidated on compaction (FR-4).
    _meter: dict = {"promptTokens": 0, "completionTokens": 0, "coveredCount": 0, "valid": False}

    def _note_usage(usage: dict | None, convo_len: int) -> None:
        """Record valid usage (FR-5): a non-error turn with prompt_tokens > 0. The
        covered prefix is the whole convo AFTER the assistant reply is appended
        (prompt tokens cover the sent messages, completion tokens the reply)."""
        pt = (usage or {}).get("prompt_tokens") or 0
        if _mode == "window" and pt and pt > 0:
            _meter.update(promptTokens=int(pt),
                          completionTokens=int((usage or {}).get("completion_tokens") or 0),
                          coveredCount=convo_len, valid=True)

    def _persist_sidecar(assistant_msg: dict, provider_items, seq: int) -> None:
        if not provider_items or provider is None:
            return
        try:
            from storage import reasoning_store
            # A reasoning encryption key is the consent to store plaintext
            # chain-of-thought (DeepSeek/Kimi). With a key, plaintext reasoning is
            # replayable and encrypted at rest; without one it's skipped (luna's
            # encrypted_content rows never need a key).
            enc_key = provider.get("reasoning_enc_key")
            # Only persist when there's replayable reasoning: a turn with only
            # message/function_call items (e.g. a responses-shaped gateway in front
            # of a model that returns no reasoning) is dead weight.
            if not reasoning_store.has_replayable_reasoning(
                    provider_items, allow_plaintext=bool(enc_key)):
                return
            origin_id = (provider.get("account_id") or provider.get("base_url") or "")
            row = reasoning_store.persist(
                session_id, seq, reasoning_store.sidecar_fingerprint(assistant_msg),
                provider_items, provider=provider.get("provider"), model=model,
                origin_id=origin_id, enc_key=enc_key)
            # ``persist`` returns None when reasoning was skipped or encryption
            # failed. Only successful writes enter the live map; this makes the
            # next Responses request replay reasoning captured earlier in this
            # same tool loop without weakening the fail-closed behavior.
            if row is not None and provider.get("stateless_reasoning_replay"):
                sidecars[seq] = row
        except Exception:
            logger.debug("reasoning sidecar persist failed", exc_info=True)

    async def _fit(convo_in: list[dict]) -> list[dict]:
        """Auto-compact (opt-in) then trim to budget. Compaction folds older turns
        into a recap; _fit_context is the always-on backstop either way."""
        nonlocal _last_compact_len
        ctx_tokens = _context_tokens(convo_in, _meter) if _mode == "window" else None
        if _mode == "window":
            # FR-19: the numeric detail rides in ``usage`` (the only free-form
            # JSON column the Debug store persists); ``text`` is the readable line.
            _measured = _meter["valid"]
            entries.append({
                "type": "context",
                "text": (f"[context] window mode · {ctx_tokens} tok / "
                         f"{context_window} ({context_window_source}) · "
                         f"{'measured' if _measured else 'estimated'} · "
                         f"compacts at {context_window - _eff_reserve}"),
                "usage": {"mode": "window", "window": context_window,
                          "source": context_window_source, "contextTokens": ctx_tokens,
                          "measured": _measured, "compactsAt": context_window - _eff_reserve},
                "timestamp": _now_iso()})
        if auto_compact:
            try:
                convo_in, ce, _last_compact_len = await _maybe_compact(
                    convo_in, model=model, provider=provider,
                    ctx_budget=ctx_budget, compact_at=compact_at,
                    last_compact_len=_last_compact_len,
                    mode=_mode, window=context_window, effective_reserve=_eff_reserve,
                    context_tokens=ctx_tokens, compact_keep=compact_keep,
                    compact_keep_amount=compact_keep_amount)
                if ce is not None:
                    entries.append(ce)
                    if ce.get("success"):
                        _meter["valid"] = False  # FR-4: stale usage after a rewrite
            except Exception:  # noqa: BLE001 — never let compaction break the loop
                logger.debug("auto-compact: unexpected failure", exc_info=True)
        return _fit_context(convo_in, fit_budget)

    compress_results = compression and COMPRESSION_ENABLED
    mcp_cap = mcp_tool_cap or MCP_TOOL_RESULT_CHARS
    wiki_cap = wiki_tool_cap or WIKI_READ_RESULT_CHARS

    # One run id per invocation, minted up front so it can be surfaced to the
    # caller (done/error events) and stamped onto activity rows for deep-linking —
    # the same id the trail is persisted under in _record's finally block.
    run_id = str(uuid.uuid4())
    _prompt_cache_key = "vw-" + hashlib.sha256(
        f"{agent_id or ''}:{session_id}".encode()).hexdigest()[:32]
    trace = {"debugRunId": run_id, "debugSessionId": session_id}
    start = _now_iso()
    entries: list[dict] = []
    # Record the FULL incoming conversation the model received — the system prompt,
    # every prior turn (history), and the current user message — as trace entries,
    # not just the first user line. On a multi-turn run (a chat channel / chat with history)
    # the old code showed the OLDEST message as "USER" and hid both the real
    # question and the prior context, so a turn that answered from history looked
    # like it invented its results with no tool calls. This run's own tool calls +
    # final answer are appended below as the loop advances.
    for m in convo:
        role = m.get("role")
        if role == "system":
            entries.append({"type": "system", "text": _text_of(m), "timestamp": start})
        elif role in ("user", "assistant"):
            if (txt := _text_of(m)):
                entries.append({"type": role, "text": txt, "timestamp": start})
        elif role == "tool":
            if (txt := _text_of(m)):
                entries.append({"type": "tool_result", "text": txt, "timestamp": start})
    final_text = ""
    tool_count = 0
    in_tokens = 0
    # Sum of every turn's prompt size across the run — kept alongside the
    # last-turn `in_tokens` snapshot so the run-level cache hit-rate can divide
    # summed cache reads by summed prompts (input_tokens keeps its last-turn
    # meaning, which other features read).
    prompt_tokens_sum = 0
    cache_read_sum = 0
    cache_write_sum = 0
    out_tokens = 0
    is_error = False
    err_text = None
    _last_err_key: str | None = None  # ponytail: simple consecutive-error detector
    _last_err_count = 0
    # Exact-repeat guard: (tool, args) -> [identical_result_streak, last_result].
    # Small local models loop on the very same failing call; a repeat whose result
    # matches the previous one gets a warning, and once the same call has returned
    # the same result twice, further repeats are NOT executed (also protects
    # side-effecting tools from a triple-send). Tools whose state moves between
    # identical calls (browser_screenshot, task_list, …) return different results,
    # which resets the streak — those are never blocked.
    _call_seen: dict[str, list] = {}
    # Retries when the model ends with an empty message. Budget of 2, tiered so
    # quality degrades only as a last resort: retry 1 keeps the configured
    # effort (with a doubled output budget when the empty turn burned its
    # budget reasoning — finish "length"); retry 2 drops to low effort.
    _final_nudges = 0
    _FINAL_NUDGE_MAX = 2
    # One-shot flags for the model call immediately after a nudge; both reset
    # after that call is built, so a retry that recovers by calling tools
    # leaves the rest of the run at its configured effort and budget.
    _nudge_low_effort = False
    _nudge_budget_boost = False
    _followthrough_nudged = False  # one retry when the model promises but doesn't act
    _wrapup_warned = False  # one pre-warn as the run approaches its step cap

    inbox = None
    try:
        # Register before yielding the id: async-generator consumers can steer
        # immediately after receiving this first event.
        try:
            inbox = run_registry.register(run_id)
        except Exception:
            logger.debug("agent loop: run registry register failed", exc_info=True)
        yield {"type": "run", "runId": run_id}

        # Guardrail semantics: None → legacy default (internal callers);
        # 0 → unlimited (the user opted out of the step cap — the backstop
        # only guards against a truly runaway loop); N → hard cap.
        _unlimited = False
        if max_iterations is None:
            _limit = MAX_ITERATIONS
        else:
            _mi = int(max_iterations)
            if _mi > 0:
                _limit = _mi
            else:
                _limit, _unlimited = UNLIMITED_BACKSTOP, True
        # Wrap-up pre-warn: N steps before the cap, tell the model to stop
        # broadening and answer with what it has. Off on the unlimited backstop
        # (there is no meaningful cap to warn about) and when disabled (0), and
        # clamped so a tiny cap still leaves at least one step of runway.
        _ww = 2 if wrapup_warn_at is None else int(wrapup_warn_at)
        effective_warn = 0 if (_unlimited or _ww <= 0) else min(_ww, _limit - 1)
        if effective_warn < 1:
            effective_warn = 0
        for _iter_idx in range(_limit):
            if inbox is not None:
                try:
                    for content in inbox.drain():
                        convo.append({"role": "user", "content": content})
                        entries.append({"type": "user", "text": content,
                                        "timestamp": _now_iso()})
                except Exception:
                    logger.debug("agent loop: run registry drain failed", exc_info=True)
            # Wrap-up pre-warn: fire at the top of the turn where, after this
            # model turn, only ``effective_warn`` tool-call turns remain — so the
            # model still has room to do one last essential lookup and answer.
            # user-role (not system) for the same chat-template reason as the
            # nudges above.
            if (effective_warn and not _wrapup_warned
                    and (_limit - _iter_idx) == effective_warn + 1):
                _wrapup_warned = True
                _warn = (
                    "[runtime notice — not the user] You have at most "
                    f"{effective_warn} tool call(s) left in this run before it is "
                    "stopped. Stop broadening your investigation. Finish only the "
                    "most essential lookup if truly needed, then give the user your "
                    "final answer based on what you have already gathered — and "
                    "honestly say what you could not verify. Do not start new lines "
                    "of investigation. If you leave anything unverified or "
                    "unfinished because of this, tell the user WHY, in the language "
                    "of their request: you are near this run's step limit, and they "
                    "can ask you to continue digging in a follow-up message.")
                convo.append({"role": "user", "content": _warn})
                entries.append({"type": "system",
                                "text": f"[wrap-up warning] {effective_warn} tool call(s) left",
                                "timestamp": _now_iso()})
            # Context management: keep the model-facing conversation within budget
            # (auto-compact first when enabled, then _fit_context as the backstop).
            convo = await _fit(convo)
            _budget = _out_budget()
            if _nudge_budget_boost:
                # The previous turn burned its whole budget reasoning — retry
                # with double the room rather than degraded effort.
                _budget *= 2
            payload = {"model": model, "messages": convo, "temperature": temperature,
                       "max_tokens": _budget}
            if sidecars:
                payload["_reasoning_sidecars"] = sidecars
            if provider is not None:
                payload["prompt_cache_key"] = _prompt_cache_key
            if top_p is not None:
                payload["top_p"] = top_p
            if top_k is not None:
                payload["top_k"] = top_k
            # Per-agent override > env default; 0/1 disables. Local upstream only —
            # remote providers reject unknown params.
            rp = repeat_penalty if repeat_penalty is not None else REPEAT_PENALTY
            if provider is None and rp and rp != 1:
                payload["repetition_penalty"] = rp  # mlx_lm.server
                payload["repeat_penalty"] = rp      # llama.cpp server
            # Reasoning effort steers remote reasoning models; egress.py translates
            # the generic key per provider. Never for the local upstream.
            # Only the SECOND empty-reply retry runs at LOW effort — a last
            # resort after a configured-effort retry (with budget boost when
            # applicable) also came back empty. Downgrade only from above
            # "low": an agent at "off" must NOT have thinking re-enabled.
            if reasoning_effort and provider is not None:
                payload["reasoning_effort"] = (
                    "low" if _nudge_low_effort
                    and reasoning_effort not in ("off", "low")
                    else reasoning_effort)
            _nudge_low_effort = False
            _nudge_budget_boost = False
            if (turn_schemas := _active_schemas()):
                payload["tools"] = turn_schemas
            if hide_reasoning and provider is None:
                # Ask thinking-capable chat templates (Qwen3-style) to skip the
                # <think> stage entirely; servers/templates without the knob
                # ignore it — the output-side strip below is the guarantee.
                payload["chat_template_kwargs"] = {"enable_thinking": False}

            # Retry only the provider stream itself. Everything below this block
            # mutates the completed turn/run state, so a failed partial attempt
            # cannot be counted or persisted as a successful turn.
            for _llm_attempt in range(max(0, config.LLM_RETRIES) + 1):
                turn = {"text": "", "tool_calls": [], "finish": None, "usage": None}
                attempt_text_yielded = False
                try:
                    async for kind, data in _stream_turn(payload, provider,
                                                         strip_think=hide_reasoning):
                        if kind == "text":
                            attempt_text_yielded = True
                            yield {"type": "text", "delta": data}
                        elif kind == "end":
                            turn = data
                    break
                except UpstreamError as exc:
                    if (not _is_transient_upstream_error(exc)
                            or _llm_attempt >= max(0, config.LLM_RETRIES)):
                        raise
                    retry_number = _llm_attempt + 1
                    entries.append({"type": "llm_retry", "text": (exc.body or "")[:200],
                                    "is_error": False, "model": model,
                                    "timestamp": _now_iso(),
                                    "provider": (provider or {}).get("provider", "local")})
                    yield {"type": "status",
                           "message": f"provider error, retrying ({retry_number}/"
                                      f"{config.LLM_RETRIES})…"}
                    if attempt_text_yielded:
                        yield {"type": "text_reset"}
                    await asyncio.sleep(config.LLM_BACKOFF_BASE * (2 ** _llm_attempt))
            final_text += turn["text"]
            usage = turn.get("usage") or {}
            # This turn's usage is attached to this turn's own assistant entry
            # (below, at each append site) so every assistant step carries its
            # own tokens/cache reads — not just once per run.
            turn_usage = _shape_usage(usage)
            cache_read = int(usage.get("cacheRead") or 0)
            cache_write = int(usage.get("cacheWrite") or 0)
            cache_read_sum += cache_read
            cache_write_sum += cache_write
            if provider is not None and (cache_read > 0 or cache_write > 0):
                try:
                    provider_catalog.note_cache_usage(provider.get("provider", ""), model,
                                                      cache_read, cache_write)
                except Exception:
                    logger.debug("cache telemetry failed", exc_info=True)
            out_tokens += usage.get("completion_tokens", 0) or 0
            if usage.get("prompt_tokens"):
                in_tokens = usage["prompt_tokens"]  # last turn's prompt size
                prompt_tokens_sum += usage["prompt_tokens"]

            if not turn["tool_calls"]:
                if turn.get("finish") == "length" and (
                        turn["text"].strip() or _final_nudges >= _FINAL_NUDGE_MAX):
                    # The provider cut generation at its token limit — say so
                    # instead of presenting a silently clipped answer as complete.
                    # A length-cut turn with NO text (a reasoning model that spent
                    # the whole budget thinking) falls through to the empty-reply
                    # nudge instead — the low-effort retry usually recovers a real
                    # answer; only a spent nudge budget lands here with the note.
                    note = ("\n\n_(stopped: hit the model's output token limit — "
                            "the answer above may be cut off)_"
                            if turn["text"].strip() else
                            "_(stopped: the model spent its entire output budget "
                            "reasoning and produced no visible answer)_")
                    final_text += note
                    yield {"type": "text", "delta": note}
                    _persist_sidecar(
                        {"role": "assistant", "content": turn["text"] or None,
                         "tool_calls": []},
                        turn.get("providerItems"), len(convo))
                    if turn["text"]:
                        entries.append(_assistant_entry(turn["text"] + note, turn_usage,
                                                        turn.get("thinking")))
                    yield {"type": "done", "outputTokens": out_tokens,
                           "runId": run_id, "sessionId": session_id}
                    return
                if not turn["text"].strip() and _final_nudges < _FINAL_NUDGE_MAX:
                    # The model went quiet — the user would see a blank reply.
                    # A bounded retry to produce a real answer. No tool_count
                    # requirement: a completely silent FIRST turn (seen with
                    # DeepSeek on a large replayed context) deserves a retry
                    # too, not a silent empty run.
                    # Sent as a user-role message: several chat templates (Qwen3.5's
                    # official one raises "System message must be at the beginning.")
                    # reject a system message mid-conversation, which 400s the whole
                    # request and kills the run with no reply at all.
                    _final_nudges += 1
                    # Tiered recovery: retry 1 keeps the configured effort (and
                    # doubles the output budget when the turn burned it all on
                    # reasoning); retry 2 drops to low effort as a last resort.
                    _nudge_low_effort = _final_nudges >= 2
                    _nudge_budget_boost = turn.get("finish") == "length"
                    if turn.get("thinking"):
                        # Keep the reasoning-only turn visible in the trace —
                        # otherwise the very turn that caused the empty reply
                        # leaves no record at all.
                        entries.append(_assistant_entry("", turn_usage, turn["thinking"]))
                    _shape = ("turn burned its output budget reasoning (finish=length)"
                              if turn.get("finish") == "length"
                              else "reasoning-only turn (thinking but no visible text)"
                              if turn.get("thinking")
                              else "turn generated tokens but no visible text"
                              if turn.get("usage")
                              else "stream carried no output at all")
                    _mode = ("at low reasoning effort" if _nudge_low_effort
                             else "with doubled output budget" if _nudge_budget_boost
                             else "at configured effort")
                    entries.append({"type": "system",
                                    "text": f"[empty-reply nudge {_final_nudges}/"
                                            f"{_FINAL_NUDGE_MAX}] {_shape}; retrying {_mode}",
                                    "timestamp": _now_iso()})
                    convo.append({"role": "user", "content": (
                        "[runtime notice — not the user] You returned an empty "
                        "message. Answer the user now in plain text based on the "
                        "conversation and any tool results above; if nothing useful "
                        "was found, say so honestly and summarize what you tried. "
                        "Do not call more tools.")})
                    continue
                if (not _followthrough_nudged and registry and turn["text"].strip()
                        and len(turn["text"]) < 600
                        and _ANNOUNCE_RE.search(turn["text"])):
                    # The model ANNOUNCED an action but called nothing — without
                    # this the run ends on a promise ("saya panggil dulu MCP…")
                    # that never gets fulfilled. One retry to make it act.
                    _followthrough_nudged = True
                    convo.append({"role": "assistant", "content": turn["text"]})
                    # user-role for the same template-compatibility reason as above
                    convo.append({"role": "user", "content": (
                        "[runtime notice — not the user] You said you would do "
                        "something but made NO tool call, and your turn is about to "
                        "end — nothing will happen after this message. Either make "
                        "the tool call NOW (if the tool isn't listed, call "
                        "search_tools to unlock it first), or give the user your "
                        "final answer. Never end your reply with a promise of "
                        "future work.")})
                    entries.append(_assistant_entry(turn["text"], turn_usage, turn.get("thinking")))
                    entries.append({"type": "system",
                                    "text": "[follow-through nudge] announced action with no tool call",
                                    "timestamp": _now_iso()})
                    continue
                _persist_sidecar(
                    {"role": "assistant", "content": turn["text"] or None,
                     "tool_calls": []},
                    turn.get("providerItems"), len(convo))
                if turn["text"]:
                    entries.append(_assistant_entry(turn["text"], turn_usage, turn.get("thinking")))
                elif turn.get("thinking"):
                    # A thinking-only final turn (nudge budget spent) still gets a
                    # trace entry — an empty run with no record of the model's last
                    # turn is undebuggable.
                    entries.append(_assistant_entry("", turn_usage, turn["thinking"]))
                if inbox is not None:
                    try:
                        pending, safe = inbox.close_if_empty()
                    except Exception:
                        logger.debug("agent loop: run registry close check failed", exc_info=True)
                        pending, safe = [], True
                    if not safe:
                        if turn["text"]:
                            convo.append({"role": "assistant", "content": turn["text"]})
                        for content in pending:
                            convo.append({"role": "user", "content": content})
                            entries.append({"type": "user", "text": content,
                                            "timestamp": _now_iso()})
                        continue
                yield {"type": "done", "outputTokens": out_tokens,
                       "runId": run_id, "sessionId": session_id}
                return

            # Record the turn's narration (content emitted alongside the tool
            # calls) so the Debug trace shows it inline, matching the chat UI —
            # otherwise a tool-calling turn's prose is dropped from the trace.
            # A text-less tool-call turn that still reasoned gets an assistant
            # entry carrying just the thinking (empty text) so captured reasoning
            # isn't invisible; ordered before this turn's tool_call entries.
            if turn["text"]:
                entries.append(_assistant_entry(turn["text"], turn_usage, turn.get("thinking")))
            elif turn.get("thinking"):
                entries.append(_assistant_entry("", turn_usage, turn.get("thinking")))
            assistant_dict = {
                "role": "assistant",
                "content": turn["text"] or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in turn["tool_calls"]
                ],
            }
            # Opt-in: replay the turn's captured reasoning on the assistant tool-call
            # message so an OpenRouter-dialect gateway sees the thinking that led to
            # the calls. Off by default — message shape is unchanged when the flag is
            # off or the turn produced no reasoning. Message keys survive egress
            # (_OPENAI_DROP only filters top-level payload keys, not message keys).
            if (provider is not None and provider.get("replay_reasoning")
                    and turn.get("thinking")):
                assistant_dict["reasoning"] = turn["thinking"]
                if turn.get("reasoning_details"):
                    assistant_dict["reasoning_details"] = turn["reasoning_details"]
            _seq = len(convo)
            convo.append(assistant_dict)
            _persist_sidecar(assistant_dict, turn.get("providerItems"), _seq)
            # Meter: this turn's usage covers the sent prompt + the reply we just
            # appended (FR-3). Estimate only anything appended after this point.
            _note_usage(usage, len(convo))

            # Parse every call's arguments up front so side-effect-free tools
            # (flagged `parallel_safe` in their registry entry) can start
            # executing concurrently, while results are still processed strictly
            # in order below — the dedup cache, consecutive-error guard, trace
            # entries, and streamed events are all order-dependent.
            parsed: list[tuple[dict, dict, str | None]] = []
            for c in turn["tool_calls"]:
                bad_json = None
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError as exc:
                    # The model emitted invalid JSON for this call's arguments —
                    # common when a string arg (e.g. wiki_write's `content`) is a
                    # large multi-line body with unescaped newlines/quotes. Don't
                    # silently drop to {} and dispatch: that surfaces as a bogus
                    # "missing required argument" error and the model retries the
                    # exact same malformed call forever. Tell it the real cause so
                    # it can re-emit valid JSON (and let the consecutive-error
                    # guard below catch a model that keeps getting it wrong).
                    args, bad_json = {}, str(exc)
                parsed.append((c, args, bad_json))
            prefetch: dict[int, asyncio.Task] = {}
            if len(parsed) > 1:
                for i, (c, args, bad_json) in enumerate(parsed):
                    if bad_json is None and (registry.get(c["name"]) or {}).get("parallel_safe"):
                        # _dispatch never raises (errors come back as strings), so
                        # an abandoned task can't leave an unretrieved exception.
                        prefetch[i] = asyncio.create_task(_dispatch(registry, c["name"], args))
            for idx, (c, args, bad_json) in enumerate(parsed):
                tool_count += 1
                entries.append({"type": "tool_call", "tool_name": c["name"],
                                "tool_call_id": c["id"], "tool_args": args, "timestamp": _now_iso()})
                yield {"type": "tool_call", "id": c["id"], "name": c["name"], "args": args,
                       "kind": (registry.get(c["name"]) or {}).get("kind")}
                # Off the event loop: this is sync SQLite on the per-tool-call
                # hot path, and it would otherwise stall the stream on each call.
                await asyncio.to_thread(_log_activity, agent_id, c["name"], args, task_id, trace=trace)
                if bad_json is not None:
                    # Same "bad arguments for <tool>" shape _dispatch emits on a
                    # TypeError, so the consecutive-error guard recognizes it too.
                    result = (f"bad arguments for {c['name']}: invalid JSON ({bad_json}). "
                              "Re-emit the call with valid JSON — escape newlines, quotes "
                              "and backslashes inside string values (this is common with a "
                              "large `content` argument).")
                else:
                    call_key = f"{c['name']}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                    rec = _call_seen.get(call_key)
                    if rec and rec[0] >= 2:
                        # Same call already returned the same result twice — don't
                        # execute a third time; hand back the cached result + a nudge.
                        stale = prefetch.pop(idx, None)
                        if stale is not None:
                            stale.cancel()
                        result = (rec[1] + "\n\n⚠ You have already made this exact call and it "
                                  "returned this same result — it was NOT executed again. Change "
                                  "the arguments or answer the user with what you already have.")
                    else:
                        task = prefetch.pop(idx, None)
                        result = await (task if task is not None
                                        else _dispatch(registry, c["name"], args))
                        # Error results are exempt: transient failures deserve
                        # re-execution, and the consecutive-error guard below
                        # already owns that loop with a more specific message.
                        if _error_shaped(c["name"], result):
                            pass
                        elif rec and result == rec[1]:
                            rec[0] += 1
                            result += ("\n\n⚠ Identical call, identical result as before. Do not "
                                       "repeat it — vary the arguments (e.g. broaden the query) or "
                                       "answer the user with what you have.")
                        else:
                            _call_seen[call_key] = [1, result]
                # Feed a capped copy back to the model (the full result is kept in the
                # trail below + the UI). MCP results get the larger MCP cap, wiki_read
                # its own wiki cap; general tools the small one. Record how many chars
                # the model did NOT see so the Debug trace can flag it — this is what
                # explains a model that "missed" a field past the cap in a big response.
                if c["name"] == "wiki_read":
                    cap = wiki_cap
                elif c["name"] == crusher.RETRIEVE_TOOL_NAME:
                    # retrieve_full pages its own output (RETRIEVE_WINDOW_CHARS)
                    # and must NEVER be re-crushed — that would mint a new hash
                    # for a windowed slice and invite hash-of-hash loops.
                    cap = crusher.RETRIEVE_WINDOW_CHARS + 300
                elif c["name"].startswith("mcp__"):
                    cap = mcp_cap
                else:
                    cap = tool_cap
                # A single result must never exceed half the whole context
                # budget — otherwise one protected recent message (e.g. a 40k
                # MCP result vs a 40k budget) makes the budget unenforceable.
                cap = min(cap, max(ctx_budget // 2, 1000))
                strategy = None
                if compress_results and c["name"] != crusher.RETRIEVE_TOOL_NAME:
                    # PRD-17: structure-aware compression instead of blind
                    # truncation. Applied ONCE, here (live zone) — the convo
                    # entry is never rewritten later, so the KV-cache prefix
                    # stays stable. Original goes to the CCR store so the
                    # model can call retrieve_full(hash) when it needs it.
                    crushed = crusher.crush(result, c["name"], cap)
                    if crushed.hash:
                        try:
                            await asyncio.to_thread(
                                ccr_store.put, crushed.hash, result,
                                c["name"], agent_id or "", session_id)
                        except Exception:
                            # Store down ⇒ the sentinel would dangle. Degrade to
                            # the legacy sentinel-less truncation (never worse
                            # than the status quo).
                            logger.warning("ccr store write failed; falling back to _cap")
                            crushed = None
                    if crushed is not None:
                        capped = crushed.text
                        truncated = crushed.saved_chars
                        strategy = crushed.strategy
                    else:
                        capped = _cap(result, cap)
                        truncated = len(result) - cap if (result and len(result) > cap) else 0
                else:
                    capped = _cap(result, cap)
                    truncated = len(result) - cap if (result and len(result) > cap) else 0
                entries.append({"type": "tool_result", "tool_name": c["name"],
                                "tool_call_id": c["id"], "tool_result": result,
                                "truncated_chars": truncated, "crush_strategy": strategy,
                                "timestamp": _now_iso()})
                yield {"type": "tool_result", "id": c["id"], "name": c["name"],
                       "result": result[:600], "resultFull": result,
                       "truncatedChars": truncated,
                       "crushStrategy": strategy}
                # Consecutive-error guard: same tool + same error prefix 3× → tell the
                # model to stop retrying instead of letting it loop to max_iterations.
                # Match every error shape the tool layer emits, anchored at the start of
                # the result (and keyed on the tool name) so a normal result that merely
                # mentions "error" isn't misread: MCP isError results ("[tool error] …",
                # mcp_client:59), and dispatch failures — "<tool> error: …" (covers both
                # built-ins and MCP session errors, mcp_client:103), "bad arguments for
                # <tool> …", and "unknown tool: …".
                _nm = c["name"]
                _is_err = _error_shaped(_nm, result)
                err_key = f"{_nm}:{result[:120]}" if _is_err else None
                if err_key and err_key == _last_err_key:
                    _last_err_count += 1
                else:
                    _last_err_key, _last_err_count = err_key, (1 if err_key else 0)
                if _last_err_count >= 3:
                    capped += ("\n\n⚠ This tool has returned the same error 3 times in a row. "
                               "Stop retrying these inputs. Report the blocker and wait for guidance.")
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": capped})

        if inbox is not None:
            try:
                inbox.close_if_empty()
            except Exception:
                logger.debug("agent loop: run registry backstop close failed", exc_info=True)
        # Conclusion turn: the step cap is spent, but don't hand back a bare
        # "stopped" note. Run ONE more model turn with the SAME payload shape as a
        # normal turn (tools included — remote translators reject a novel shape)
        # to synthesize a real final answer from what was gathered. Any tool calls
        # it emits are ignored (nothing more will execute); only its text is kept.
        _fallback = "\n\n_(answer may be incomplete — hit the step limit)_"
        _concluded = False
        try:
            _notice = (
                "[runtime notice — not the user] This run has reached its "
                "tool-step limit — NO more tool calls will be executed, including "
                "any you emit now. Reply to the user now, in the same language as "
                "their request: state that you hit the step limit, give your best "
                "answer from what you already gathered, and say clearly what is "
                "missing or unverified. Do not promise future work.")
            convo = await _fit(convo)
            convo.append({"role": "user", "content": _notice})
            entries.append({"type": "system", "text": f"[step-limit conclusion] {_notice}",
                            "timestamp": _now_iso()})
            payload = {"model": model, "messages": convo, "temperature": temperature,
                       "max_tokens": _out_budget()}
            if sidecars:
                payload["_reasoning_sidecars"] = sidecars
            if provider is not None:
                payload["prompt_cache_key"] = _prompt_cache_key
            if top_p is not None:
                payload["top_p"] = top_p
            if top_k is not None:
                payload["top_k"] = top_k
            rp = repeat_penalty if repeat_penalty is not None else REPEAT_PENALTY
            if provider is None and rp and rp != 1:
                payload["repetition_penalty"] = rp
                payload["repeat_penalty"] = rp
            if reasoning_effort and provider is not None:
                payload["reasoning_effort"] = reasoning_effort
            if (turn_schemas := _active_schemas()):
                payload["tools"] = turn_schemas
            if hide_reasoning and provider is None:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            turn = {"text": "", "tool_calls": [], "finish": None, "usage": None}
            async for kind, data in _stream_turn(payload, provider,
                                                 strip_think=hide_reasoning):
                if kind == "text":
                    yield {"type": "text", "delta": data}
                elif kind == "end":
                    turn = data
            usage = turn.get("usage") or {}
            turn_usage = _shape_usage(usage)
            cache_read = int(usage.get("cacheRead") or 0)
            cache_write = int(usage.get("cacheWrite") or 0)
            cache_read_sum += cache_read
            cache_write_sum += cache_write
            if provider is not None and (cache_read > 0 or cache_write > 0):
                try:
                    provider_catalog.note_cache_usage(provider.get("provider", ""), model,
                                                      cache_read, cache_write)
                except Exception:
                    logger.debug("cache telemetry failed", exc_info=True)
            out_tokens += usage.get("completion_tokens", 0) or 0
            if usage.get("prompt_tokens"):
                in_tokens = usage["prompt_tokens"]
                prompt_tokens_sum += usage["prompt_tokens"]
            _persist_sidecar(
                {"role": "assistant", "content": turn["text"] or None,
                 "tool_calls": []},
                turn.get("providerItems"), len(convo))
            if turn["text"].strip():
                final_text += turn["text"]
                entries.append(_assistant_entry(turn["text"], turn_usage, turn.get("thinking")))
                _concluded = True
        except Exception:
            # A provider error (or anything else) in the conclusion turn degrades
            # to the deterministic suffix — never a crashed/hung run.
            logger.warning("agent loop: step-limit conclusion turn failed", exc_info=True)
        if not _concluded:
            # No usable answer from the conclusion turn — degrade to a
            # deterministic suffix so the run still tells the user something.
            final_text += _fallback
            yield {"type": "text", "delta": _fallback}
        yield {"type": "done", "outputTokens": out_tokens,
               "runId": run_id, "sessionId": session_id}
    except UpstreamError as exc:
        is_error, err_text = True, exc.body[:500]
        yield {"type": "error", "message": exc.body[:500], "runId": run_id, "sessionId": session_id}
    except Exception as exc:  # noqa: BLE001
        is_error, err_text = True, str(exc)
        logger.warning("agent loop error: %s", exc)
        yield {"type": "error", "message": str(exc), "runId": run_id, "sessionId": session_id}
    finally:
        try:
            run_registry.unregister(run_id)
        except Exception:
            logger.debug("agent loop: run registry unregister failed", exc_info=True)
        # Record the failure as a visible trace step, not just run-level metadata —
        # otherwise the Debug trace shows the tool calls and then just ends, with no
        # sign of *why* the turn stopped. The UI renders an ``llm_fallback`` entry as
        # an "Upstream provider error" block (see debug-page RunCard).
        if is_error and err_text:
            entries.append({"type": "llm_fallback", "text": err_text, "is_error": True,
                            "model": model, "timestamp": _now_iso(),
                            "provider": (provider or {}).get("provider", "local")})
        # Off the event loop: persisting the full trace (prompts + tool results)
        # is sync SQLite that would otherwise block the loop at end-of-turn.
        await asyncio.to_thread(
            _record, run_id, session_id, model, start, entries, final_text, tool_count,
            in_tokens, out_tokens, is_error, err_text,
            provider=(provider or {}).get("provider", "local"), agent_id=agent_id,
            cache_target=provider, prompt_tokens_sum=prompt_tokens_sum,
            cache_read_sum=cache_read_sum, cache_write_sum=cache_write_sum,
            reasoning_effort=(reasoning_effort if (reasoning_effort and provider is not None)
                              else None))


def _wire_diagnostics(cache_target: dict | None, reasoning_effort: str | None) -> dict:
    """Non-secret wire evidence for the Debug trace: cache capability flags plus
    the provider-compat reasoning experiment fields (thinking_format,
    replay_reasoning, and the effective ``reasoning`` body param the openrouter
    dialect would send). Never credentials or message content."""
    out: dict = {}
    if cache_target:
        for key in ("prompt_cache_key", "cache_control", "prompt_cache_retention"):
            if cache_target.get(key):
                out[key] = cache_target.get(key)
        thinking_format = cache_target.get("thinking_format")
        if thinking_format is None and cache_target.get("provider") == "openai-compatible":
            # An unset custom row is the UI's OpenAI default; keep the target
            # field unset for storage compatibility but report the effective
            # dialect in the trace.
            thinking_format = "openai"
        if thinking_format:
            out["thinking_format"] = thinking_format
            # Mirror egress._openai_body per dialect so the trace shows the
            # param actually sent: OpenAI uses top-level reasoning_effort only
            # when the custom gateway opted into forwarding it; zai has no
            # effort dial, only the on/off type.
            if cache_target.get("kind") == "responses":
                if reasoning_effort:
                    out["reasoning"] = {
                        "effort": "none" if reasoning_effort == "off"
                        else reasoning_effort}
            elif thinking_format == "zai":
                if reasoning_effort:
                    out["thinking"] = {"type": "disabled" if reasoning_effort == "off"
                                       else "enabled"}
            elif thinking_format == "openai":
                if reasoning_effort:
                    if (reasoning_effort != "off"
                            and cache_target.get("reasoning_effort", True)):
                        out["reasoning_effort"] = reasoning_effort
            elif reasoning_effort:
                out["reasoning"] = ({"effort": "none"} if reasoning_effort == "off"
                                    else {"effort": reasoning_effort})
        if cache_target.get("replay_reasoning"):
            out["replay_reasoning"] = True
    return out


def _record(run_id, session_id, model, start, entries, final_text, tool_count,
            in_tokens, out_tokens, is_error, err_text, provider="local", agent_id=None,
            reasoning_effort=None, cache_target=None, prompt_tokens_sum=0,
            cache_read_sum=None, cache_write_sum=None):
    """Persist the loop's trail as one Debug run (best-effort)."""
    # Sum each assistant turn's cache reads/writes so the run header can show a
    # cache hit-rate — the middleware path fills these for API-captured calls, so
    # mirror its key names (cache_read/cache_write) here for loop-recorded runs.
    # The loop supplies direct sums because a textless tool-call turn has usage
    # but intentionally creates no empty assistant trace entry. Reconstruct from
    # entries only for older/direct callers that do not provide the sums.
    if cache_read_sum is None or cache_write_sum is None:
        cache_read = 0
        cache_write = 0
        for e in entries:
            u = e.get("usage") or {}
            if e.get("type") == "assistant" and u:
                cache_read += int(u.get("cacheRead") or 0)
                cache_write += int(u.get("cacheWrite") or 0)
    else:
        cache_read = int(cache_read_sum)
        cache_write = int(cache_write_sum)
    try:
        debug_db.insert_run({
            "run_id": run_id, "session_id": session_id, "agent_id": agent_id,
            "start_time": start, "end_time": _now_iso(),
            "method": "POST", "path": "/api/chat", "endpoint": "/api/chat",
            "provider": provider, "model": model, "status_code": 500 if is_error else 200,
            "is_error": is_error, "error_text": err_text, "stream": True,
            "tool_calls": tool_count,
            "input_tokens": in_tokens, "output_tokens": out_tokens,
            # Summed per-turn prompt sizes (input_tokens stays last-turn); lets the
            # run-level cache hit-rate divide summed reads by summed prompts.
            "prompt_tokens_sum": prompt_tokens_sum,
            "cache_read": cache_read, "cache_write": cache_write,
            "total_tokens": (prompt_tokens_sum or in_tokens or 0) + (out_tokens or 0),
            # The run preview should show the CURRENT question, not the oldest
            # history line — so take the last user entry (this turn's prompt).
            "first_user_text": next((e["text"] for e in reversed(entries) if e["type"] == "user"), None),
            "last_assistant_text": final_text or None,
            # Keep only target-side capability flags for computed Debug
            # diagnostics; never persist credentials, messages, or the full request.
            "request_json": json.dumps(_wire_diagnostics(cache_target, reasoning_effort)),
            "reasoning_effort": reasoning_effort,
        }, entries)
    except Exception:
        logger.debug("agent loop: debug record failed", exc_info=True)
