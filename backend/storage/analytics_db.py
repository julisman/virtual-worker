# Cost & Usage Analytics (PRD-06) — read + aggregate layer over the debug tables.

"""Historical analytics over the captured LLM-call facts in ``app.db``.

This module is **read-only** over ``debug_runs`` (written by ApiDebugMiddleware).
It never captures new data and never recomputes cost — cost is read exactly as
captured (hosted models priced, local = $0). All aggregation happens in SQL via
``GROUP BY`` where possible; agent attribution is resolved in Python because a
session maps to an agent through several indirect routes:

  - ``worker:<agentId>``    -> the agent id is embedded in the session id
  - ``chat:<id>``           -> join ``chat_sessions`` (if present) for ``agent_id``
  - ``schedule:<id>``       -> join ``schedules`` (if present) for ``agent_id``
  - ``<provider>:<model>``  -> an external/adhoc caller, no local agent

Everything degrades gracefully: missing tables/columns fall back to "external"
or "unknown" rather than raising, so analytics works on a bare debug DB too.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from storage import debug_db

# Dimensions that map cleanly to a single ``debug_runs`` column.
_COLUMN_DIMENSIONS = {
    "model": "model",
    "provider": "provider",
    "endpoint": "endpoint",
    "session": "session_id",
}


# --------------------------------------------------------------------------- #
# Window / filter helpers
# --------------------------------------------------------------------------- #

def _bounds(frm: Optional[str], to: Optional[str]) -> tuple[str, str]:
    """Inclusive ISO string bounds for a ``YYYY-MM-DD`` window.

    ``start_time`` is a tz-aware ISO string (e.g. ``2026-06-28T10:00:00+00:00``);
    lexicographic comparison against these bounds is correct for the prefix we
    care about. Open-ended when a side is omitted.
    """
    lo = f"{frm}T00:00:00" if frm else "0000-00-00"
    hi = f"{to}T23:59:59.999999" if to else "9999-99-99"
    return lo, hi


def _bucket_expr(bucket: str) -> str:
    """SQL expression for the day/week bucket key over ``start_time``."""
    if bucket == "week":
        # Monday of the ISO week the start_time falls in.
        return "date(start_time, 'weekday 0', '-6 days')"
    return "substr(start_time, 1, 10)"  # day: YYYY-MM-DD


# --------------------------------------------------------------------------- #
# Agent attribution
# --------------------------------------------------------------------------- #

def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _schedule_id(session_id: str) -> str:
    """"schedule:<id>" or "schedule:<id>.<n>" → "<id>"."""
    return session_id.split(":", 1)[1].split(".", 1)[0]


def _session_agent_map(conn, session_ids: set[str]) -> dict[str, Optional[str]]:
    """Map each session id to a local agent id (or None for external/adhoc)."""
    out: dict[str, Optional[str]] = {}
    chat_ids: set[str] = set()
    sched_ids: set[str] = set()
    for sid in session_ids:
        if sid.startswith("worker:"):
            out[sid] = sid.split(":", 1)[1] or None
        elif sid.startswith("chat:") or sid.count(":") >= 2:
            # in-app chat and chat-channel sessions both live in chat_sessions
            chat_ids.add(sid)
            out[sid] = None
        elif sid.startswith("schedule:"):
            sched_ids.add(_schedule_id(sid))
            out[sid] = None
        else:
            out[sid] = None  # external (provider:model) or adhoc

    # chat:<id> -> chat_sessions.agent_id (the chat session id IS the full session id).
    if chat_ids and _table_exists(conn, "chat_sessions"):
        rows = conn.execute(
            f"SELECT id, agent_id FROM chat_sessions WHERE id IN "
            f"({','.join('?' * len(chat_ids))})",
            tuple(chat_ids),
        ).fetchall()
        for r in rows:
            if r["agent_id"]:
                out[r["id"]] = r["agent_id"]

    # schedule:<id> -> schedules.agent_id
    if sched_ids and _table_exists(conn, "schedules"):
        rows = conn.execute(
            f"SELECT id, agent_id FROM schedules WHERE id IN "
            f"({','.join('?' * len(sched_ids))})",
            tuple(sched_ids),
        ).fetchall()
        by_id = {r["id"]: r["agent_id"] for r in rows}
        for sid in session_ids:
            if sid.startswith("schedule:"):
                aid = by_id.get(_schedule_id(sid))
                if aid:
                    out[sid] = aid
    return out


def _agent_session_ids(conn, agent_id: str) -> set[str]:
    """Return known debug sessions attributable to one agent."""
    sessions = {
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM debug_runs").fetchall()
    }
    worker_prefix = f"worker:{agent_id}"
    out = {sid for sid in sessions if sid.startswith(worker_prefix)}

    if _table_exists(conn, "chat_sessions"):
        rows = conn.execute(
            "SELECT id FROM chat_sessions WHERE agent_id = ?", (agent_id,)
        ).fetchall()
        out.update(r["id"] for r in rows)
    if _table_exists(conn, "schedules"):
        rows = conn.execute(
            "SELECT id FROM schedules WHERE agent_id = ?", (agent_id,)
        ).fetchall()
        sched = {r["id"] for r in rows}
        # A schedule's runs live under "schedule:<id>" or, after a context reset /
        # in fresh-session mode, "schedule:<id>.<n>" (see schedule_store.session_id_for).
        out.update(sid for sid in sessions
                   if sid.startswith("schedule:") and _schedule_id(sid) in sched)
        out.update(f"schedule:{i}" for i in sched)
    return out


def _agent_names(conn, agent_ids: set[str]) -> dict[str, str]:
    """Resolve agent ids to display names (best-effort)."""
    names: dict[str, str] = {}
    real = {a for a in agent_ids if a}
    if real and _table_exists(conn, "agents"):
        rows = conn.execute(
            f"SELECT id, name FROM agents WHERE id IN "
            f"({','.join('?' * len(real))})",
            tuple(real),
        ).fetchall()
        names = {r["id"]: (r["name"] or r["id"]) for r in rows}
    return names


# --------------------------------------------------------------------------- #
# Aggregations
# --------------------------------------------------------------------------- #

def _round_cost(v: Any) -> float:
    return round(float(v or 0.0), 6)


def _filter_sql(filters: dict[str, Any], conn=None) -> tuple[str, list[Any]]:
    """Build SQL filters, including an attribution-aware agent session set."""
    clauses: list[str] = []
    params: list[Any] = []
    for key in ("provider", "model", "endpoint"):
        val = filters.get(key)
        if val:
            clauses.append(f"{key} = ?")
            params.append(val)
    agent_id = filters.get("agent")
    if agent_id and conn is not None:
        session_ids = _agent_session_ids(conn, str(agent_id))
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            clauses.append(f"session_id IN ({placeholders})")
            params.extend(sorted(session_ids))
        else:
            clauses.append("1 = 0")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def summary(frm: Optional[str] = None, to: Optional[str] = None,
            filters: Optional[dict] = None) -> dict[str, Any]:
    """Top KPI tiles for the window: calls, tokens, cost, errors, avg latency."""
    filters = filters or {}
    lo, hi = _bounds(frm, to)
    conn = debug_db.get_connection()
    try:
        extra, fparams = _filter_sql(filters, conn)
        r = conn.execute(
            f"""
            SELECT
                COUNT(*)                   AS calls,
                COUNT(DISTINCT session_id) AS sessions,
                SUM(input_tokens)          AS input_tokens,
                SUM(output_tokens)         AS output_tokens,
                SUM(total_tokens)          AS total_tokens,
                SUM(cost)                  AS cost,
                SUM(is_error)              AS errors,
                SUM(tool_calls)            AS tool_calls,
                AVG(duration_ms)           AS avg_latency
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            """,
            [lo, hi, *fparams],
        ).fetchone()
        latency_rows = conn.execute(
            f"""
            SELECT duration_ms
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
              AND duration_ms IS NOT NULL
            ORDER BY duration_ms ASC
            """,
            [lo, hi, *fparams],
        ).fetchall()
        latencies = [row["duration_ms"] for row in latency_rows]
        p95 = (int(latencies[min(len(latencies) - 1,
                                math.ceil(0.95 * len(latencies)) - 1)])
               if latencies else None)
        calls = r["calls"] or 0
        errors = r["errors"] or 0
        return {
            "from": frm,
            "to": to,
            "calls": calls,
            "sessions": r["sessions"] or 0,
            "inputTokens": r["input_tokens"] or 0,
            "outputTokens": r["output_tokens"] or 0,
            "totalTokens": r["total_tokens"] or 0,
            "cost": _round_cost(r["cost"]),
            "errors": errors,
            "errorRate": round(errors / calls, 4) if calls else 0.0,
            "toolCalls": r["tool_calls"] or 0,
            "avgLatencyMs": round(r["avg_latency"]) if r["avg_latency"] is not None else None,
            "p95LatencyMs": p95,
        }
    finally:
        conn.close()


def trends(frm: Optional[str] = None, to: Optional[str] = None,
           bucket: str = "day", filters: Optional[dict] = None) -> list[dict[str, Any]]:
    """Per-bucket (day|week) time series over the window."""
    filters = filters or {}
    bucket = "week" if bucket == "week" else "day"
    lo, hi = _bounds(frm, to)
    bexpr = _bucket_expr(bucket)
    conn = debug_db.get_connection()
    try:
        extra, fparams = _filter_sql(filters, conn)
        rows = conn.execute(
            f"""
            SELECT
                {bexpr}            AS bucket,
                COUNT(*)           AS calls,
                SUM(input_tokens)  AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens)  AS total_tokens,
                SUM(cost)          AS cost,
                SUM(is_error)      AS errors,
                AVG(duration_ms)   AS avg_latency
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            [lo, hi, *fparams],
        ).fetchall()
        out = []
        for r in rows:
            calls = r["calls"] or 0
            errors = r["errors"] or 0
            out.append({
                "date": r["bucket"],
                "calls": calls,
                "inputTokens": r["input_tokens"] or 0,
                "outputTokens": r["output_tokens"] or 0,
                "totalTokens": r["total_tokens"] or 0,
                "cost": _round_cost(r["cost"]),
                "errors": errors,
                "errorRate": round(errors / calls, 4) if calls else 0.0,
                "avgLatencyMs": round(r["avg_latency"]) if r["avg_latency"] is not None else None,
            })
        return out
    finally:
        conn.close()


def breakdown(dimension: str, frm: Optional[str] = None, to: Optional[str] = None,
              filters: Optional[dict] = None) -> list[dict[str, Any]]:
    """Aggregate table grouped by a dimension (agent|model|provider|endpoint|session)."""
    filters = filters or {}
    if dimension == "agent":
        return _breakdown_by_agent(frm, to, filters)
    col = _COLUMN_DIMENSIONS.get(dimension)
    if not col:
        raise ValueError(f"unknown dimension: {dimension}")
    lo, hi = _bounds(frm, to)
    extra, fparams = _filter_sql(filters)
    conn = debug_db.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT
                {col}              AS key,
                COUNT(*)           AS calls,
                COUNT(DISTINCT session_id) AS sessions,
                SUM(total_tokens)  AS total_tokens,
                SUM(cost)          AS cost,
                SUM(is_error)      AS errors,
                SUM(tool_calls)    AS tool_calls,
                AVG(duration_ms)   AS avg_latency
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            GROUP BY {col}
            ORDER BY calls DESC
            """,
            [lo, hi, *fparams],
        ).fetchall()
        out = []
        for r in rows:
            calls = r["calls"] or 0
            errors = r["errors"] or 0
            out.append({
                "key": r["key"] or "unknown",
                "calls": calls,
                "sessions": r["sessions"] or 0,
                "totalTokens": r["total_tokens"] or 0,
                "cost": _round_cost(r["cost"]),
                "errors": errors,
                "errorRate": round(errors / calls, 4) if calls else 0.0,
                "toolCalls": r["tool_calls"] or 0,
                "avgLatencyMs": round(r["avg_latency"]) if r["avg_latency"] is not None else None,
            })
        return out
    finally:
        conn.close()


def _breakdown_by_agent(frm, to, filters) -> list[dict[str, Any]]:
    """Agent breakdown — resolves session->agent in Python, then aggregates."""
    cards = scorecard(frm, to, filters=filters)
    return [{
        "key": c["agentId"] or "external",
        "name": c["name"],
        "calls": c["calls"],
        "sessions": c["sessions"],
        "totalTokens": c["totalTokens"],
        "cost": c["cost"],
        "errors": c["errors"],
        "errorRate": c["errorRate"],
        "toolCalls": c["toolCalls"],
        "avgLatencyMs": c["avgLatencyMs"],
    } for c in cards]


def scorecard(frm: Optional[str] = None, to: Optional[str] = None,
              agent_id: Optional[str] = None,
              filters: Optional[dict] = None) -> list[dict[str, Any]]:
    """Per-agent virtual worker scorecards over the window.

    Runs are grouped per session in SQL, attributed to an agent in Python, then
    rolled up per agent. Sessions with no local agent are bucketed under the
    synthetic ``external`` card so totals still reconcile.
    """
    filters = filters or {}
    lo, hi = _bounds(frm, to)
    extra, fparams = _filter_sql(filters)
    conn = debug_db.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT
                session_id,
                COUNT(*)           AS calls,
                SUM(total_tokens)  AS total_tokens,
                SUM(cost)          AS cost,
                SUM(is_error)      AS errors,
                SUM(tool_calls)    AS tool_calls,
                SUM(duration_ms)   AS dur_sum,
                SUM(CASE WHEN duration_ms IS NOT NULL THEN 1 ELSE 0 END) AS dur_n,
                COUNT(DISTINCT substr(start_time, 1, 10)) AS active_days
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            GROUP BY session_id
            """,
            [lo, hi, *fparams],
        ).fetchall()

        session_ids = {r["session_id"] for r in rows}
        amap = _session_agent_map(conn, session_ids)

        agg: dict[str, dict[str, Any]] = {}
        active_days: dict[str, set[str]] = {}
        for r in rows:
            aid = amap.get(r["session_id"])
            key = aid or "external"
            a = agg.setdefault(key, {
                "agentId": aid, "calls": 0, "sessions": 0, "totalTokens": 0,
                "cost": 0.0, "errors": 0, "toolCalls": 0, "_durSum": 0, "_durN": 0,
            })
            a["calls"] += r["calls"] or 0
            a["sessions"] += 1
            a["totalTokens"] += r["total_tokens"] or 0
            a["cost"] += r["cost"] or 0.0
            a["errors"] += r["errors"] or 0
            a["toolCalls"] += r["tool_calls"] or 0
            a["_durSum"] += r["dur_sum"] or 0
            a["_durN"] += r["dur_n"] or 0

        # Distinct active days per agent (separate pass, attribution-aware).
        day_rows = conn.execute(
            f"""
            SELECT session_id, substr(start_time, 1, 10) AS day
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            GROUP BY session_id, day
            """,
            [lo, hi, *fparams],
        ).fetchall()
        for dr in day_rows:
            key = (amap.get(dr["session_id"]) or "external")
            active_days.setdefault(key, set()).add(dr["day"])

        names = _agent_names(conn, {k for k in agg if k != "external"})
        out = []
        for key, a in agg.items():
            calls = a["calls"]
            errors = a["errors"]
            out.append({
                "agentId": a["agentId"],
                "name": names.get(key, "External / adhoc" if key == "external" else key),
                "calls": calls,
                "sessions": a["sessions"],
                "totalTokens": a["totalTokens"],
                "cost": _round_cost(a["cost"]),
                "errors": errors,
                "errorRate": round(errors / calls, 4) if calls else 0.0,
                "toolCalls": a["toolCalls"],
                "avgLatencyMs": round(a["_durSum"] / a["_durN"]) if a["_durN"] else None,
                "activeDays": len(active_days.get(key, set())),
            })
        out.sort(key=lambda c: c["calls"], reverse=True)
        if agent_id:
            out = [c for c in out if c["agentId"] == agent_id]
        return out
    finally:
        conn.close()


def export_runs(frm: Optional[str] = None, to: Optional[str] = None,
                filters: Optional[dict] = None) -> list[dict[str, Any]]:
    """Flat list of runs in the window for CSV/JSON export (respects filters)."""
    filters = filters or {}
    lo, hi = _bounds(frm, to)
    extra, fparams = _filter_sql(filters)
    conn = debug_db.get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT run_id, session_id, start_time, endpoint, provider, model,
                   status_code, is_error, input_tokens, output_tokens,
                   total_tokens, cost, tool_calls, duration_ms
            FROM debug_runs
            WHERE start_time >= ? AND start_time <= ?{extra}
            ORDER BY start_time ASC
            """,
            [lo, hi, *fparams],
        ).fetchall()
        amap = _session_agent_map(conn, {r["session_id"] for r in rows})
        out = []
        for r in rows:
            out.append({
                "runId": r["run_id"],
                "sessionId": r["session_id"],
                "agentId": amap.get(r["session_id"]) or "",
                "startTime": r["start_time"],
                "endpoint": r["endpoint"] or "",
                "provider": r["provider"] or "",
                "model": r["model"] or "",
                "statusCode": r["status_code"],
                "isError": int(r["is_error"] or 0),
                "inputTokens": r["input_tokens"] or 0,
                "outputTokens": r["output_tokens"] or 0,
                "totalTokens": r["total_tokens"] or 0,
                "cost": _round_cost(r["cost"]),
                "toolCalls": r["tool_calls"] or 0,
                "durationMs": r["duration_ms"],
            })
        return out
    finally:
        conn.close()
