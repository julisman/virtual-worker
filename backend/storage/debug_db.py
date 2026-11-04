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
from datetime import datetime, timezone
from typing import Any, Optional

from config import db_path as _db_path, ensure_dir

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
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO debug_runs (
                run_id, session_id, start_time, end_time, duration_ms,
                method, path, endpoint, provider, model, status_code,
                is_error, error_text, stream, input_tokens, output_tokens,
                cache_read, cache_write, total_tokens, cost, tool_calls,
                first_user_text, last_assistant_text, request_json,
                response_text, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run["run_id"], run["session_id"], run["start_time"], run.get("end_time"),
                run.get("duration_ms"), run.get("method"), run.get("path"), run.get("endpoint"),
                run.get("provider"), run.get("model"), run.get("status_code"),
                1 if run.get("is_error") else 0, run.get("error_text"),
                1 if run.get("stream") else 0, run.get("input_tokens", 0), run.get("output_tokens", 0),
                run.get("cache_read", 0), run.get("cache_write", 0), run.get("total_tokens", 0),
                run.get("cost", 0.0), run.get("tool_calls", 0), run.get("first_user_text"),
                run.get("last_assistant_text"), run.get("request_json"), run.get("response_text"),
                run.get("created_at") or _now_iso(),
            ),
        )
        for seq, e in enumerate(entries):
            conn.execute(
                """
                INSERT OR REPLACE INTO debug_entries (
                    id, run_id, session_id, seq, timestamp, type, text, thinking,
                    tool_name, tool_call_id, tool_args, tool_result, is_error,
                    model, provider, usage_json, stop_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.get("id") or str(uuid.uuid4()), run["run_id"], run["session_id"], seq,
                    e.get("timestamp") or run["start_time"], e.get("type", "system"), e.get("text"),
                    e.get("thinking"), e.get("tool_name"), e.get("tool_call_id"),
                    json.dumps(e["tool_args"]) if e.get("tool_args") is not None else None,
                    e.get("tool_result"), 1 if e.get("is_error") else 0, e.get("model"),
                    e.get("provider"), json.dumps(e["usage"]) if e.get("usage") is not None else None,
                    e.get("stop_reason"),
                ),
            )
        conn.commit()
    except Exception:
        logger.exception("debug_db: failed to insert run %s", run.get("run_id"))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Read path (called by routes/debug.py)
# --------------------------------------------------------------------------- #

def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


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
                "SELECT model, provider, first_user_text, last_assistant_text "
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
                "firstMessage": last_run["first_user_text"] if last_run else None,
                "lastMessage": last_run["last_assistant_text"] if last_run else None,
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
                "endpoint": rr["endpoint"],
                "stream": bool(rr["stream"]),
                "statusCode": rr["status_code"],
                "entries": entries,
                "totalCost": rr["cost"] or 0.0,
                "totalTokens": rr["total_tokens"] or 0,
                "inputTokens": rr["input_tokens"] or 0,
                "outputTokens": rr["output_tokens"] or 0,
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
        return {"runs": runs, "totalEntries": total_entries, "summary": sess_summary}
    finally:
        conn.close()


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
