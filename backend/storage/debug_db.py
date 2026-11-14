# API-Call Debugging — trace storage.

"""SQLite storage for captured LLM API-call traces.

Raw sqlite3, per-call connections, WAL mode, ``Row`` factory, idempotent schema,
writing into ``app.db``. A "run" is one inbound API call to the inference/chat
endpoints; "entries" are the readable steps inside it (user prompt, assistant
reply, tool calls, errors).
"""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import db_path as _db_path, ensure_dir
from gateway.pricing import estimate_cost

logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS debug_runs (
            run_id              TEXT PRIMARY KEY,
            session_id          TEXT NOT NULL,
            start_time          TEXT NOT NULL,
            end_time            TEXT,
            duration_ms         INTEGER,
            method              TEXT,
            path                TEXT,
            endpoint            TEXT,
            provider            TEXT,
            model               TEXT,
            status_code         INTEGER,
            is_error            INTEGER DEFAULT 0,
            error_text          TEXT,
            stream              INTEGER DEFAULT 0,
            input_tokens        INTEGER DEFAULT 0,
            output_tokens       INTEGER DEFAULT 0,
            cache_read          INTEGER DEFAULT 0,
            cache_write         INTEGER DEFAULT 0,
            total_tokens        INTEGER DEFAULT 0,
            cost                REAL DEFAULT 0,
            tool_calls          INTEGER DEFAULT 0,
            first_user_text     TEXT,
            last_assistant_text TEXT,
            request_json        TEXT,
            response_text       TEXT,
            created_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS debug_entries (
            id           TEXT PRIMARY KEY,
            run_id       TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            seq          INTEGER NOT NULL,
            timestamp    TEXT NOT NULL,
            type         TEXT NOT NULL,
            text         TEXT,
            thinking     TEXT,
            tool_name    TEXT,
            tool_call_id TEXT,
            tool_args    TEXT,
            tool_result  TEXT,
            is_error     INTEGER DEFAULT 0,
            model        TEXT,
            provider     TEXT,
            usage_json   TEXT,
            stop_reason  TEXT,
            FOREIGN KEY (run_id) REFERENCES debug_runs (run_id) ON DELETE CASCADE
        )
        """
    )
    # Guarded migration: which agent ran this call (for per-agent Debug filtering).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(debug_runs)").fetchall()}
    if "agent_id" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN agent_id TEXT")
    # Guarded migration: reasoning effort the run was sent with (low/medium/high/
    # xhigh; NULL = not set / local model). Effort displaces temperature on
    # external reasoning models, so a slow or expensive run needs this to be
    # explainable from the trace.
    if "reasoning_effort" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN reasoning_effort TEXT")
    # Guarded migration: chat-channel end-to-end and time-to-first-push
    # timings, populated after the turn is delivered.
    if "channel_e2e_ms" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN channel_e2e_ms INTEGER")
    if "ttft_ms" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN ttft_ms INTEGER")
    # Guarded migration: summed per-turn prompt sizes for the run (input_tokens
    # stays the LAST turn's prompt). The run-level cache hit-rate divides summed
    # cache reads by this so it can't clamp to a bogus 100%.
    if "prompt_tokens_sum" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN prompt_tokens_sum INTEGER DEFAULT 0")
    # Guarded migration: context-window usage. ``peak_context_tokens`` is the
    # LARGEST single-call prompt in the run (how full the window got — unlike the
    # summed prompt_tokens_sum, which counts the same history once per call);
    # ``context_window`` is the model's window the run was measured against
    # (NULL = unknown); ``context_series`` is a JSON list of every call's prompt
    # size, in order, so the Debug UI can chart how the context grew.
    if "peak_context_tokens" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN peak_context_tokens INTEGER DEFAULT 0")
    if "context_window" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN context_window INTEGER")
    if "context_series" not in cols:
        conn.execute("ALTER TABLE debug_runs ADD COLUMN context_series TEXT")
    # Guarded migration: chars of a tool result withheld from the model (capped to
    # fit context). 0 = the model saw the whole result. Surfaced in the trace so a
    # "missing field" is explainable.
    ent_cols = {r["name"] for r in conn.execute("PRAGMA table_info(debug_entries)").fetchall()}
    if "truncated_chars" not in ent_cols:
        conn.execute("ALTER TABLE debug_entries ADD COLUMN truncated_chars INTEGER DEFAULT 0")
    # Guarded migration (PRD-17): which crusher path produced a tool result —
    # passthrough | json | lines | headtail; NULL = compression off / pre-PRD-17.
    if "crush_strategy" not in ent_cols:
        conn.execute("ALTER TABLE debug_entries ADD COLUMN crush_strategy TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_runs_session ON debug_runs (session_id, start_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_entries_run ON debug_entries (run_id, seq)")
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """Open app.db with WAL mode; create the debug schema once per process."""
    global _schema_ready
    db_path = _db_path()
    ensure_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                try:
                    _ensure_schema(conn)
                    _schema_ready = True
                except Exception:
                    conn.close()
                    raise
    return conn


# --------------------------------------------------------------------------- #
# Write path (called by the api_debug middleware)
# --------------------------------------------------------------------------- #

def insert_run(run: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Persist one captured API call (run + its entries). Best-effort: any
    failure is logged and swallowed so capture never affects the request."""
    try:
        conn = get_connection()
    except Exception:
        logger.exception("debug_db: failed to open connection")
        return
    # Cost is ALWAYS computed here from the shared price table — never trusted
    # from callers (any incoming ``cost`` key is ignored). Agent-loop runs bill
    # every turn's prompt, summed in ``prompt_tokens_sum``; middleware single-call
    # runs carry no/zero ``prompt_tokens_sum`` and fall back to ``input_tokens``.
    # Unknown/local models resolve to 0.0 from the table lookup.
    run_cost = estimate_cost(
        run.get("model"),
        run.get("prompt_tokens_sum") or run.get("input_tokens") or 0,
        run.get("output_tokens") or 0,
    )
    duration_ms = run.get("duration_ms")
    if duration_ms is None and run.get("start_time") and run.get("end_time"):
        try:
            started = datetime.fromisoformat(str(run["start_time"]).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(run["end_time"]).replace("Z", "+00:00"))
            duration_ms = max(0, round((ended - started).total_seconds() * 1000))
        except (TypeError, ValueError):
            duration_ms = None
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO debug_runs (
                run_id, session_id, start_time, end_time, duration_ms,
                method, path, endpoint, provider, model, status_code,
                is_error, error_text, stream, input_tokens, output_tokens,
                cache_read, cache_write, total_tokens, cost, tool_calls,
                first_user_text, last_assistant_text, request_json,
                response_text, agent_id, reasoning_effort, prompt_tokens_sum,
                peak_context_tokens, context_window, context_series,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run["run_id"], run["session_id"], run["start_time"], run.get("end_time"),
                duration_ms, run.get("method"), run.get("path"), run.get("endpoint"),
                run.get("provider"), run.get("model"), run.get("status_code"),
                1 if run.get("is_error") else 0, run.get("error_text"),
                1 if run.get("stream") else 0, run.get("input_tokens", 0), run.get("output_tokens", 0),
                run.get("cache_read", 0), run.get("cache_write", 0), run.get("total_tokens", 0),
                run_cost, run.get("tool_calls", 0), run.get("first_user_text"),
                run.get("last_assistant_text"), run.get("request_json"), run.get("response_text"),
                run.get("agent_id"), run.get("reasoning_effort"),
                run.get("prompt_tokens_sum", 0),
                # A single-call (middleware) capture's prompt IS its context size.
                int(run.get("peak_context_tokens") or run.get("input_tokens") or 0),
                run.get("context_window") or None,
                json.dumps([int(x) for x in run["context_series"]])
                if run.get("context_series") else None,
                run.get("created_at") or _now_iso(),
            ),
        )
        for seq, e in enumerate(entries):
            conn.execute(
                """
                INSERT OR REPLACE INTO debug_entries (
                    id, run_id, session_id, seq, timestamp, type, text, thinking,
                    tool_name, tool_call_id, tool_args, tool_result, is_error,
                    model, provider, usage_json, stop_reason, truncated_chars,
                    crush_strategy
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.get("id") or str(uuid.uuid4()), run["run_id"], run["session_id"], seq,
                    e.get("timestamp") or run["start_time"], e.get("type", "system"), e.get("text"),
                    e.get("thinking"), e.get("tool_name"), e.get("tool_call_id"),
                    json.dumps(e["tool_args"]) if e.get("tool_args") is not None else None,
                    e.get("tool_result"), 1 if e.get("is_error") else 0, e.get("model"),
                    e.get("provider"), json.dumps(e["usage"]) if e.get("usage") is not None else None,
                    e.get("stop_reason"), int(e.get("truncated_chars") or 0),
                    e.get("crush_strategy"),
                ),
            )
        conn.commit()
    except Exception:
        logger.exception("debug_db: failed to insert run %s", run.get("run_id"))
    finally:
        conn.close()


def update_run_timing(run_id, *, e2e_ms=None, ttft_ms=None) -> None:
    """Best-effort update of post-delivery chat-channel timing fields."""
    if not run_id or (e2e_ms is None and ttft_ms is None):
        return
    try:
        conn = get_connection()
    except Exception:
        logger.debug("debug_db: failed to open connection for timing update", exc_info=True)
        return
    try:
        updates: list[str] = []
        params: list[Any] = []
        if e2e_ms is not None:
            updates.append("channel_e2e_ms = COALESCE(?, channel_e2e_ms)")
            params.append(e2e_ms)
        if ttft_ms is not None:
            updates.append("ttft_ms = COALESCE(?, ttft_ms)")
            params.append(ttft_ms)
        if updates:
            conn.execute(
                f"UPDATE debug_runs SET {', '.join(updates)} WHERE run_id = ?",
                [*params, run_id],
            )
            conn.commit()
    except Exception:
        logger.debug("debug_db: failed to update timing for run %s", run_id, exc_info=True)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Read path (called by routes/debug.py)
# --------------------------------------------------------------------------- #

def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


_CACHE_REMOTE_PROVIDERS = {"anthropic", "openai", "openai-compatible", "opencode-go", "codex"}


def _context_fields(rr: sqlite3.Row) -> dict[str, Any]:
    """Context-window usage for one run row. Older rows predate the columns:
    their ``input_tokens`` (the last call's prompt) stands in for the peak."""
    keys = rr.keys()
    peak = (rr["peak_context_tokens"] if "peak_context_tokens" in keys else 0) or 0
    series: list[int] = []
    if "context_series" in keys and rr["context_series"]:
        try:
            series = [int(x) for x in json.loads(rr["context_series"])]
        except (TypeError, ValueError):
            series = []
    return {
        "peakContextTokens": peak or (rr["input_tokens"] or 0),
        "contextWindow": rr["context_window"] if "context_window" in keys else None,
        "contextSeries": series,
    }


def _cache_stats(rr: sqlite3.Row, run: dict[str, Any]) -> dict[str, int]:
    """Read normalized cache usage from either loop entries or middleware rows."""
    read = int(rr["cache_read"] or 0)
    write = int(rr["cache_write"] or 0)
    prompt = int(rr["input_tokens"] or 0)
    for entry in reversed(run.get("entries") or []):
        usage = entry.get("usage") or {}
        if entry.get("type") == "assistant" and usage:
            read = max(read, int(usage.get("cacheRead") or 0))
            write = max(write, int(usage.get("cacheWrite") or 0))
            prompt = max(prompt, int(usage.get("input") or usage.get("prompt_tokens") or 0))
            break
    return {"read": read, "write": write, "prompt": prompt}


def _cache_flags(rr: sqlite3.Row) -> bool:
    try:
        req = json.loads(rr["request_json"] or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(req, dict):
        return False
    if req.get("prompt_cache_key") or req.get("prompt_cache_retention"):
        return True
    raw = json.dumps(req, separators=(",", ":"))
    return "cache_control" in raw


def _entry_tool_set(run: dict[str, Any]) -> set[str]:
    return {str(e.get("toolName")) for e in run.get("entries") or []
            if e.get("type") == "tool_call" and e.get("toolName")}


def _history_rewritten(run: dict[str, Any]) -> bool:
    for entry in run.get("entries") or []:
        text = str(entry.get("text") or "").lower()
        if entry.get("type") == "context" and ("compact" in text or "rewrite" in text):
            return True
        if "elided to fit" in text or "compacted" in text:
            return True
    return False


def _cache_gap_seconds(rr: sqlite3.Row, previous: sqlite3.Row | None) -> float | None:
    if not previous:
        return None
    try:
        now = datetime.fromisoformat(str(rr["start_time"]).replace("Z", "+00:00"))
        then = datetime.fromisoformat(str(previous["start_time"]).replace("Z", "+00:00"))
        return max(0.0, (now - then).total_seconds())
    except (TypeError, ValueError):
        return None


def _cache_cost(model: str | None, tokens: int) -> float:
    """Input-priced cost of rebilled tokens, reusing the shared price table
    (the single source of truth for per-model cost)."""
    return estimate_cost(model, tokens, 0)


def _annotate_cache_misses(run_rows: list[sqlite3.Row], runs: list[dict[str, Any]]) -> None:
    """Annotate likely remote cache misses from already captured trace data.

    This is intentionally computed on Debug reads: no new table, background loop,
    or provider-specific billing state is needed. The first remote turn is never
    warned; subsequent zero/low reads are compared with the prior turn in this
    session and labeled using the strongest available signal.
    """
    previous: tuple[sqlite3.Row, dict[str, Any], dict[str, int]] | None = None
    for rr, run in zip(run_rows, runs):
        provider = (rr["provider"] or "").lower()
        stats = _cache_stats(rr, run)
        if provider not in _CACHE_REMOTE_PROVIDERS or previous is None:
            if provider in _CACHE_REMOTE_PROVIDERS:
                previous = (rr, run, stats)
            continue
        prev_rr, prev_run, prev_stats = previous
        has_previous_activity = prev_stats["read"] > 0 or prev_stats["write"] > 0
        low_read = stats["read"] == 0 or (
            prev_stats["prompt"] > 0 and stats["read"] < max(1, prev_stats["prompt"] // 4)
        )
        should_warn = low_read and (has_previous_activity or _cache_flags(rr))
        if should_warn:
            cause = "cache prefix changed"
            if (rr["provider"] or "") != (prev_rr["provider"] or "") or rr["model"] != prev_rr["model"]:
                cause = "model/provider switch"
            else:
                gap = _cache_gap_seconds(rr, prev_rr)
                ttl = 300 if provider == "anthropic" else 600
                if gap is not None and gap > ttl:
                    cause = f"idle gap > {ttl // 60}m TTL"
                elif _entry_tool_set(run) != _entry_tool_set(prev_run):
                    cause = "tool-set change"
                elif _history_rewritten(run):
                    cause = "history rewrite (compaction)"
            rebilled = max(0, stats["prompt"] - stats["read"])
            miss = {
                "rebilledTokens": rebilled,
                "estimatedCost": _cache_cost(rr["model"], rebilled),
                "cause": cause,
            }
            run["cacheMiss"] = miss
            for entry in reversed(run.get("entries") or []):
                if entry.get("type") == "assistant":
                    entry["cacheMiss"] = miss
                    break
        previous = (rr, run, stats)


def list_sessions() -> list[dict[str, Any]]:
    """Aggregate runs into sessions (newest activity first)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                session_id,
                COUNT(*)                       AS runs,
                SUM(tool_calls)                AS tool_calls,
                SUM(is_error)                  AS error_count,
                SUM(total_tokens)              AS total_tokens,
                SUM(cost)                      AS total_cost,
                MAX(COALESCE(NULLIF(peak_context_tokens, 0), input_tokens, 0)) AS peak_ctx,
                MIN(start_time)                AS first_time,
                MAX(start_time)                AS last_time
            FROM debug_runs
            GROUP BY session_id
            ORDER BY last_time DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            last_run = conn.execute(
                "SELECT model, provider, agent_id, first_user_text, last_assistant_text, "
                "COALESCE(NULLIF(peak_context_tokens, 0), input_tokens, 0) AS last_ctx, "
                "context_window "
                "FROM debug_runs WHERE session_id = ? ORDER BY start_time DESC LIMIT 1",
                (r["session_id"],),
            ).fetchone()
            out.append({
                "sessionId": r["session_id"],
                "runs": r["runs"],
                "toolCalls": r["tool_calls"] or 0,
                "errorCount": r["error_count"] or 0,
                "totalTokens": r["total_tokens"] or 0,
                "totalCost": r["total_cost"] or 0.0,
                "firstTime": r["first_time"],
                "lastTime": r["last_time"],
                "model": last_run["model"] if last_run else None,
                "provider": last_run["provider"] if last_run else None,
                "agentId": last_run["agent_id"] if last_run else None,
                "firstMessage": last_run["first_user_text"] if last_run else None,
                "lastMessage": last_run["last_assistant_text"] if last_run else None,
                # Context-window usage: the biggest single prompt any run in the
                # session sent, the latest run's peak, and the latest run's window.
                "peakContextTokens": r["peak_ctx"] or 0,
                "lastContextTokens": (last_run["last_ctx"] or 0) if last_run else 0,
                "contextWindow": last_run["context_window"] if last_run else None,
            })
        return out
    finally:
        conn.close()


def summary() -> dict[str, Any]:
    conn = get_connection()
    try:
        r = conn.execute(
            """
            SELECT
                COUNT(DISTINCT session_id) AS sessions,
                COUNT(*)                   AS runs,
                SUM(tool_calls)            AS tool_calls,
                SUM(is_error)              AS errors,
                SUM(total_tokens)          AS tokens,
                SUM(cost)                  AS cost,
                MAX(start_time)            AS last_activity
            FROM debug_runs
            """
        ).fetchone()
        providers = {
            row["provider"] or "unknown": row["c"]
            for row in conn.execute(
                "SELECT provider, COUNT(*) AS c FROM debug_runs GROUP BY provider"
            ).fetchall()
        }
        return {
            "totalSessions": r["sessions"] or 0,
            "totalRuns": r["runs"] or 0,
            "totalToolCalls": r["tool_calls"] or 0,
            "totalErrors": r["errors"] or 0,
            "totalTokens": r["tokens"] or 0,
            "totalCost": r["cost"] or 0.0,
            "providers": providers,
            "lastActivity": r["last_activity"],
        }
    finally:
        conn.close()


def get_session(session_id: str) -> dict[str, Any]:
    """Return all runs (with entries) for a session, plus a per-session summary."""
    conn = get_connection()
    try:
        run_rows = conn.execute(
            "SELECT * FROM debug_runs WHERE session_id = ? ORDER BY start_time ASC",
            (session_id,),
        ).fetchall()
        runs = []
        total_entries = 0
        models: set[str] = set()
        providers: set[str] = set()
        tool_counter: dict[str, int] = {}
        durations: list[int] = []
        for rr in run_rows:
            ent_rows = conn.execute(
                "SELECT * FROM debug_entries WHERE run_id = ? ORDER BY seq ASC",
                (rr["run_id"],),
            ).fetchall()
            entries = []
            tool_breakdown: dict[str, int] = {}
            for er in ent_rows:
                e = {
                    "id": er["id"],
                    "timestamp": er["timestamp"],
                    "type": er["type"],
                    "text": er["text"],
                    "thinking": er["thinking"],
                    "toolName": er["tool_name"],
                    "toolCallId": er["tool_call_id"],
                    "toolArgs": json.loads(er["tool_args"]) if er["tool_args"] else None,
                    "toolResult": er["tool_result"],
                    "isError": bool(er["is_error"]),
                    "model": er["model"],
                    "provider": er["provider"],
                    "usage": json.loads(er["usage_json"]) if er["usage_json"] else None,
                    "stopReason": er["stop_reason"],
                    "truncatedChars": (er["truncated_chars"] if "truncated_chars" in er.keys() else 0) or 0,
                    "crushStrategy": er["crush_strategy"] if "crush_strategy" in er.keys() else None,
                }
                entries.append(e)
                if er["type"] == "tool_call" and er["tool_name"]:
                    tool_breakdown[er["tool_name"]] = tool_breakdown.get(er["tool_name"], 0) + 1
                    tool_counter[er["tool_name"]] = tool_counter.get(er["tool_name"], 0) + 1
            total_entries += len(entries)
            if rr["model"]:
                models.add(rr["model"])
            if rr["provider"]:
                providers.add(rr["provider"])
            if rr["duration_ms"] is not None:
                durations.append(rr["duration_ms"])
            runs.append({
                "runId": rr["run_id"],
                "startTime": rr["start_time"],
                "endTime": rr["end_time"],
                "durationMs": rr["duration_ms"],
                "model": rr["model"],
                "provider": rr["provider"],
                "reasoningEffort": (rr["reasoning_effort"]
                                    if "reasoning_effort" in rr.keys() else None),
                "endpoint": rr["endpoint"],
                "stream": bool(rr["stream"]),
                "statusCode": rr["status_code"],
                "entries": entries,
                "totalCost": rr["cost"] or 0.0,
                "totalTokens": rr["total_tokens"] or 0,
                "inputTokens": rr["input_tokens"] or 0,
                "promptTokensSum": (rr["prompt_tokens_sum"]
                                    if "prompt_tokens_sum" in rr.keys() else 0) or 0,
                "outputTokens": rr["output_tokens"] or 0,
                **_context_fields(rr),
                "cacheRead": rr["cache_read"] or 0,
                "cacheWrite": rr["cache_write"] or 0,
                "toolCalls": rr["tool_calls"] or 0,
                "errorCount": 1 if rr["is_error"] else 0,
                "toolBreakdown": tool_breakdown,
                "firstUserText": rr["first_user_text"],
                "lastAssistantText": rr["last_assistant_text"],
            })

        error_count = sum(1 for rr in run_rows if rr["is_error"])
        first_time = run_rows[0]["start_time"] if run_rows else None
        last_time = run_rows[-1]["start_time"] if run_rows else None
        top_tools = sorted(
            ({"name": k, "count": v} for k, v in tool_counter.items()),
            key=lambda x: x["count"], reverse=True,
        )
        sess_summary = {
            "runs": len(runs),
            "totalEntries": total_entries,
            "errorCount": error_count,
            "firstTime": first_time,
            "lastTime": last_time,
            "durationMs": sum(durations) if durations else None,
            "avgRunMs": round(sum(durations) / len(durations)) if durations else None,
            "maxRunMs": max(durations) if durations else None,
            "models": sorted(models),
            "providers": sorted(providers),
            "topTools": top_tools,
        }
        _annotate_cache_misses(run_rows, runs)
        return {"runs": runs, "totalEntries": total_entries, "summary": sess_summary}
    finally:
        conn.close()


def tools_used(run_id: str) -> list[str]:
    """Names of the tools a run actually called, in call order. Used to annotate
    chat history so the model sees that a reply's details came from tool calls —
    without this, multi-tool replies read back as pure narration and teach the
    model to fabricate results instead of calling tools (self-imitation)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT tool_name FROM debug_entries WHERE run_id = ? AND type = 'tool_call' "
            "ORDER BY rowid", (run_id,)).fetchall()
    finally:
        conn.close()
    return [r["tool_name"] for r in rows if r["tool_name"]]


def run_model(run_id: str) -> tuple[Optional[str], Optional[str]]:
    """The ``(model, provider)`` recorded for one run, or ``(None, None)`` —
    a lightweight lookup for stamping audit fields (e.g. onto a case) without
    building the whole session like ``get_run`` does."""
    conn = get_connection()
    try:
        r = conn.execute(
            "SELECT model, provider FROM debug_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None, None
    return r["model"], r["provider"]


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """Resolve a single run by id → ``{sessionId, run}`` (or ``None``).

    Lets a deep-link that only carries a run id (e.g. an activity row) find both
    the run and the session it lives under. Reuses ``get_session``'s run builder."""
    conn = get_connection()
    try:
        r = conn.execute(
            "SELECT session_id FROM debug_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    session_id = r["session_id"]
    data = get_session(session_id)
    run = next((x for x in data["runs"] if x["runId"] == run_id), None)
    if not run:
        return None
    return {"sessionId": session_id, "run": run}


def sum_review_cost_today(now: datetime | None = None) -> float:
    """Return today's cost for the stateless review-loop calls.

    Review calls use a dedicated ``review:`` session prefix.  This helper is
    intentionally defensive so a partially migrated/empty debug database never
    prevents normal processing.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    midnight = current.astimezone(timezone.utc).replace(hour=0, minute=0, second=0,
                                                         microsecond=0).isoformat()
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) AS total FROM debug_runs "
                "WHERE session_id LIKE 'review:%' AND start_time >= ?", (midnight,)
            ).fetchone()
            return float(row["total"] or 0.0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.debug("debug_db: review cost lookup failed", exc_info=True)
        return 0.0


def clear(session_id: Optional[str] = None) -> int:
    conn = get_connection()
    try:
        if session_id:
            cur = conn.execute("DELETE FROM debug_runs WHERE session_id = ?", (session_id,))
        else:
            cur = conn.execute("DELETE FROM debug_runs")
        conn.execute("DELETE FROM debug_entries WHERE run_id NOT IN (SELECT run_id FROM debug_runs)")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_runs(run_ids: list[str]) -> int:
    """Delete selected runs and their entries, returning runs removed."""
    ids = [str(run_id) for run_id in run_ids if run_id]
    if not ids:
        return 0
    conn = get_connection()
    try:
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(f"DELETE FROM debug_runs WHERE run_id IN ({marks})", ids)
        conn.execute("DELETE FROM debug_entries WHERE run_id NOT IN (SELECT run_id FROM debug_runs)")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def prune(*, max_age_days: int | None = None, max_rows: int | None = None,
          protected_run_ids: set[str] | None = None) -> int:
    """Bound the trace DB (capture writes full prompts + tool results on every
    LLM call, so an always-on studio grows it without limit). Delete runs older
    than ``max_age_days``, then, if still over ``max_rows``, drop the oldest
    beyond that ceiling. Orphaned entries are cleaned up. 0/None disables a
    bound. Returns the number of runs deleted."""
    conn = get_connection()
    deleted = 0
    protected_table = False
    try:
        protected = sorted(str(run_id) for run_id in (protected_run_ids or set()) if run_id)
        if protected:
            conn.execute("CREATE TEMP TABLE _protected_runs (run_id TEXT PRIMARY KEY)")
            for start in range(0, len(protected), 5000):
                conn.executemany(
                    "INSERT OR IGNORE INTO _protected_runs (run_id) VALUES (?)",
                    [(run_id,) for run_id in protected[start:start + 5000]],
                )
            protected_table = True
        protected_clause = (" AND run_id NOT IN (SELECT run_id FROM _protected_runs)"
                            if protected_table else "")

        if max_age_days and max_age_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            deleted += conn.execute(
                "DELETE FROM debug_runs WHERE created_at < ?" + protected_clause,
                (cutoff,)).rowcount
        if max_rows and max_rows > 0:
            deleted += conn.execute(
                "DELETE FROM debug_runs WHERE run_id NOT IN "
                "(SELECT run_id FROM debug_runs ORDER BY created_at DESC LIMIT ?)" + protected_clause,
                (max_rows,)).rowcount
        conn.execute("DELETE FROM debug_entries WHERE run_id NOT IN (SELECT run_id FROM debug_runs)")
        conn.commit()
        return deleted
    finally:
        if protected_table:
            conn.execute("DROP TABLE IF EXISTS _protected_runs")
        conn.close()
