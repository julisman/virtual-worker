"""Evaluation suites: persistence for suites, runs and results (PRD §8.14).

A **suite** is a named, ordered list of **cases**. A case is a prompt (plus an
optional system message) and how to score the answer:

  * ``expected``      exact / substring match after normalization
  * ``rubric``        list of keywords; score = fraction present
  * ``judge``         instructions for an LLM judge (``expected`` doubles as the
                      reference answer); score 0..1 with a rationale
  * ``tools`` +       the model must emit a tool call whose name matches
    ``expectedTool``  ``expectedTool.name`` and whose arguments contain
                      ``expectedTool.args``; score 1 or 0
  * ``scorer``        "auto" (pick from the fields above, in the order
                      tool > judge > expected > rubric) or one of
                      exact | rubric | judge | tool to force one

A **run** executes one suite across several on-device models (each activated in
turn) with options (temperature, maxTokens, ctx, repetitions, judge). Each
(model, case, repetition) produces one **result** row: output, score, latency,
tokens, resident memory of the model server, judge rationale, tool calls.

Three tables in ``app.db`` following the storage convention (WAL, ISO
timestamps, camelCase dicts out). The leaderboard aggregation lives here so the
routes and the runner share one source of truth.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir
from storage import _sqlite

_lock = threading.Lock()

RUN_STATUSES = ("pending", "running", "done", "canceled", "error")
SCORERS = ("auto", "exact", "rubric", "judge", "tool", "none")
TASK_TYPES = ("qa", "summarization", "tool_use", "coding", "freeform")
SUITE_FORMAT = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_suites (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            items_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
            id TEXT PRIMARY KEY,
            suite_id TEXT NOT NULL,
            suite_name TEXT NOT NULL DEFAULT '',
            suite_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            models_json TEXT NOT NULL DEFAULT '[]',
            options_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            model TEXT NOT NULL,
            item_index INTEGER NOT NULL,
            rep INTEGER NOT NULL DEFAULT 0,
            output TEXT NOT NULL DEFAULT '',
            score REAL,
            scored INTEGER NOT NULL DEFAULT 0,
            scorer TEXT,
            rationale TEXT,
            tool_calls_json TEXT,
            latency_ms REAL,
            ttft_ms REAL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            tokens INTEGER,
            tokens_per_sec REAL,
            rss_bytes INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_runs_suite ON eval_runs(suite_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id)")
    conn.mark_initialized()
    return conn


# --------------------------------------------------------------------------- #
# Row mappers + case normalization
# --------------------------------------------------------------------------- #

def _suite_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "name": r["name"], "description": r["description"],
        "items": json.loads(r["items_json"] or "[]"), "version": r["version"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def _run_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "suiteId": r["suite_id"], "suiteName": r["suite_name"],
        "suiteVersion": r["suite_version"], "status": r["status"],
        "models": json.loads(r["models_json"] or "[]"),
        "options": json.loads(r["options_json"] or "{}"),
        "progress": json.loads(r["progress_json"]) if r["progress_json"] else None,
        "error": r["error"], "createdAt": r["created_at"],
        "startedAt": r["started_at"], "finishedAt": r["finished_at"],
    }


def _result_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "runId": r["run_id"], "model": r["model"], "itemIndex": r["item_index"],
        "rep": r["rep"], "output": r["output"], "score": r["score"], "scored": bool(r["scored"]),
        "scorer": r["scorer"], "rationale": r["rationale"],
        "toolCalls": json.loads(r["tool_calls_json"]) if r["tool_calls_json"] else None,
        "latencyMs": r["latency_ms"], "ttftMs": r["ttft_ms"],
        "promptTokens": r["prompt_tokens"], "completionTokens": r["completion_tokens"],
        "tokens": r["tokens"], "tokensPerSec": r["tokens_per_sec"], "rssBytes": r["rss_bytes"],
        "error": r["error"], "createdAt": r["created_at"],
    }


def _keywords(raw) -> list[str]:
    if isinstance(raw, str):
        return [k.strip() for k in raw.split(",") if k.strip()]
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    return []


def normalize_items(items: list | None) -> list[dict]:
    """Keep only the fields a case understands; coerce types defensively."""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        prompt = str(it.get("prompt") or "").strip()
        if not prompt:
            continue
        case: dict = {"prompt": prompt}
        if it.get("system"):
            case["system"] = str(it["system"])
        if it.get("expected"):
            case["expected"] = str(it["expected"])
        rubric = _keywords(it.get("rubric"))
        if rubric:
            case["rubric"] = rubric
        if it.get("judge"):
            case["judge"] = str(it["judge"]).strip()
        tools = it.get("tools")
        if isinstance(tools, list) and tools:
            case["tools"] = [t for t in tools if isinstance(t, dict)]
        et = it.get("expectedTool")
        if isinstance(et, dict) and et.get("name"):
            args = et.get("args") if isinstance(et.get("args"), dict) else {}
            case["expectedTool"] = {"name": str(et["name"]), "args": args}
        scorer = str(it.get("scorer") or "auto")
        case["scorer"] = scorer if scorer in SCORERS else "auto"
        tt = str(it.get("taskType") or "")
        if tt in TASK_TYPES:
            case["taskType"] = tt
        out.append(case)
    return out


def effective_scorer(case: dict) -> str:
    """Which scorer a case uses once "auto" is resolved."""
    s = case.get("scorer") or "auto"
    if s != "auto":
        return s
    if case.get("expectedTool"):
        return "tool"
    if case.get("judge"):
        return "judge"
    if case.get("expected"):
        return "exact"
    if case.get("rubric"):
        return "rubric"
    return "none"


# --------------------------------------------------------------------------- #
# Suites
# --------------------------------------------------------------------------- #

def create_suite(name: str, description: str = "", items: list | None = None) -> dict:
    sid = "es_" + uuid.uuid4().hex[:10]
    now = _now_iso()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO eval_suites (id, name, description, items_json, version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, (name or "Untitled").strip() or "Untitled", (description or "").strip(),
             json.dumps(normalize_items(items)), 1, now, now),
        )
    return get_suite(sid)  # type: ignore[return-value]


def get_suite(suite_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM eval_suites WHERE id=?", (suite_id,)).fetchone()
    return _suite_row(r) if r else None


def list_suites() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM eval_suites ORDER BY updated_at DESC").fetchall()
    return [_suite_row(r) for r in rows]


def update_suite(suite_id: str, *, name: str | None = None,
                 description: str | None = None, items: list | None = None) -> dict | None:
    cur = get_suite(suite_id)
    if not cur:
        return None
    name = (name if name is not None else cur["name"]).strip() or "Untitled"
    description = (description if description is not None else cur["description"]).strip()
    new_items = normalize_items(items) if items is not None else cur["items"]
    bump = 1 if (items is not None and new_items != cur["items"]) else 0   # a changed case list is a new version
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE eval_suites SET name=?, description=?, items_json=?, version=version+?, updated_at=? WHERE id=?",
            (name, description, json.dumps(new_items), bump, _now_iso(), suite_id),
        )
    return get_suite(suite_id)


def delete_suite(suite_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM eval_suites WHERE id=?", (suite_id,))
        return cur.rowcount > 0


def export_suite(suite_id: str) -> dict | None:
    s = get_suite(suite_id)
    if not s:
        return None
    return {"format": "virtual-worker-eval-suite", "formatVersion": SUITE_FORMAT,
            "name": s["name"], "description": s["description"], "version": s["version"],
            "items": s["items"], "exportedAt": _now_iso()}


def import_suite(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("expected a suite object with an items list")
    items = normalize_items(data["items"])
    if not items:
        raise ValueError("the suite has no usable cases (every case needs a prompt)")
    return create_suite(str(data.get("name") or "Imported suite"), str(data.get("description") or ""), items)


def suite_from_feedback(agent_id: str | None = None, name: str | None = None, limit: int = 200) -> dict:
    """Turn up-rated and corrected chat replies into judge-scored cases: the
    person's prompt is the case, the kept reply is the reference answer."""
    from storage import feedback_store
    rows = feedback_store.list_feedback(agent_id=agent_id, limit=limit)
    items: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if r["signal"] == "up":
            ref = r["original"]
        elif r["signal"] == "edit":
            ref = r["corrected"] or ""
        else:
            continue
        prompt, ref = (r["userPrompt"] or "").strip(), (ref or "").strip()
        if not prompt or not ref or prompt in seen:
            continue
        seen.add(prompt)
        items.append({"prompt": prompt, "expected": ref, "scorer": "judge",
                      "judge": "Score how well the output matches the reference answer in substance and tone. "
                               "Wording may differ; facts, decisions and omissions matter."})
    if not items:
        raise ValueError("no usable feedback yet: rate a reply up or save a correction first")
    return create_suite(name or f"From feedback ({len(items)} cases)",
                        "Built from up-rated and corrected replies; scored by an LLM judge against the kept reply.",
                        items)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def normalize_options(options: dict | None) -> dict:
    o = dict(options or {})
    out: dict = {}
    try:
        out["temperature"] = max(0.0, min(2.0, float(o.get("temperature", 0.0))))
    except (TypeError, ValueError):
        out["temperature"] = 0.0
    try:
        out["maxTokens"] = max(1, min(8192, int(o.get("maxTokens", 512))))
    except (TypeError, ValueError):
        out["maxTokens"] = 512
    try:
        out["repetitions"] = max(1, min(5, int(o.get("repetitions", 1))))
    except (TypeError, ValueError):
        out["repetitions"] = 1
    if o.get("ctx"):
        try:
            out["ctx"] = int(o["ctx"])
        except (TypeError, ValueError):
            pass
    judge = o.get("judge")
    if isinstance(judge, dict) and judge.get("mode") in ("self", "provider"):
        out["judge"] = {"mode": judge["mode"],
                        "provider": str(judge.get("provider") or ""),
                        "model": str(judge.get("model") or "")}
    elif judge == "self":
        out["judge"] = {"mode": "self", "provider": "", "model": ""}
    if o.get("seed") is not None:
        try:
            out["seed"] = int(o["seed"])
        except (TypeError, ValueError):
            pass
    return out


def create_run(suite_id: str, models: list, options: dict | None = None) -> dict:
    suite = get_suite(suite_id)
    if not suite:
        raise ValueError("no such suite")
    if not suite["items"]:
        raise ValueError("the suite has no cases")
    models = [m for m in (models or []) if isinstance(m, dict) and m.get("path")]
    if not models:
        raise ValueError("pick at least one model to compare")
    rid = "er_" + uuid.uuid4().hex[:10]
    now = _now_iso()
    opts = normalize_options(options)
    progress = {"modelIndex": 0, "modelTotal": len(models), "itemIndex": 0,
                "itemTotal": len(suite["items"]) * opts["repetitions"],
                "currentModel": None, "phase": "pending"}
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO eval_runs (id, suite_id, suite_name, suite_version, status, models_json, "
            "options_json, progress_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, suite_id, suite["name"], suite["version"], "pending", json.dumps(models),
             json.dumps(opts), json.dumps(progress), now),
        )
    return get_run(rid)  # type: ignore[return-value]


def get_run(run_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
    return _run_row(r) if r else None


def list_runs(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_run_row(r) for r in rows]


def set_status(run_id: str, status: str, *, error: str | None = None,
               started: bool = False, finished: bool = False) -> None:
    sets, vals = ["status=?"], [status]
    if error is not None:
        sets.append("error=?"); vals.append(error)
    if started:
        sets.append("started_at=?"); vals.append(_now_iso())
    if finished:
        sets.append("finished_at=?"); vals.append(_now_iso())
    vals.append(run_id)
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE eval_runs SET {', '.join(sets)} WHERE id=?", vals)


def set_progress(run_id: str, progress: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE eval_runs SET progress_json=? WHERE id=?", (json.dumps(progress), run_id))


def delete_run(run_id: str) -> bool:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM eval_results WHERE run_id=?", (run_id,))
        cur = conn.execute("DELETE FROM eval_runs WHERE id=?", (run_id,))
        return cur.rowcount > 0


def mark_stale_runs() -> int:
    """Runs left pending/running by a previous process can never finish."""
    with _lock, _conn() as conn:
        cur = conn.execute("UPDATE eval_runs SET status='error', error='interrupted by a restart', finished_at=? "
                           "WHERE status IN ('pending','running')", (_now_iso(),))
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

def add_result(run_id: str, model: str, item_index: int, *, rep: int = 0, output: str = "",
               score: float | None = None, scored: bool = False, scorer: str | None = None,
               rationale: str | None = None, tool_calls: list | None = None,
               latency_ms: float | None = None, ttft_ms: float | None = None,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               tokens: int | None = None, tokens_per_sec: float | None = None,
               rss_bytes: int | None = None, error: str | None = None) -> dict:
    rid = "rr_" + uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO eval_results (id, run_id, model, item_index, rep, output, score, scored, scorer, "
            "rationale, tool_calls_json, latency_ms, ttft_ms, prompt_tokens, completion_tokens, tokens, "
            "tokens_per_sec, rss_bytes, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, run_id, model, item_index, rep, (output or "")[:20000], score, 1 if scored else 0, scorer,
             (rationale or None), json.dumps(tool_calls) if tool_calls else None, latency_ms, ttft_ms,
             prompt_tokens, completion_tokens, tokens, tokens_per_sec, rss_bytes, error, _now_iso()),
        )
    with _conn() as conn:
        r = conn.execute("SELECT * FROM eval_results WHERE id=?", (rid,)).fetchone()
    return _result_row(r)


def list_results(run_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM eval_results WHERE run_id=? ORDER BY model, item_index, rep",
                            (run_id,)).fetchall()
    return [_result_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# Leaderboard aggregation
# --------------------------------------------------------------------------- #

def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def leaderboard(run_id: str) -> list[dict]:
    """Per model: average quality over scored cases, tool-call reliability over
    tool cases, throughput, latency, tokens, peak resident memory, errors.
    Sorted best first; the first entry is the winner."""
    results = list_results(run_id)
    by_model: dict[str, list[dict]] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    rows: list[dict] = []
    for model, rs in by_model.items():
        scores = [r["score"] for r in rs if r["scored"] and r["score"] is not None]
        tool_scores = [r["score"] for r in rs if r["scorer"] == "tool" and r["score"] is not None]
        judge_scores = [r["score"] for r in rs if r["scorer"] == "judge" and r["score"] is not None]
        latencies = [r["latencyMs"] for r in rs if r["latencyMs"] is not None and not r["error"]]
        tps = [r["tokensPerSec"] for r in rs if r["tokensPerSec"] is not None and not r["error"]]
        rss = [r["rssBytes"] for r in rs if r["rssBytes"]]
        errors = sum(1 for r in rs if r["error"])
        rows.append({
            "model": model, "cases": len(rs), "scoredCases": len(scores),
            "avgScore": _mean(scores),
            "toolReliability": _mean(tool_scores), "toolCases": len(tool_scores),
            "judgeScore": _mean(judge_scores), "judgeCases": len(judge_scores),
            "avgTokensPerSec": _mean(tps), "avgLatencyMs": _mean(latencies),
            "totalTokens": sum(r["tokens"] or 0 for r in rs),
            "peakMemoryBytes": max(rss) if rss else None,
            "errors": errors, "errorRate": round(errors / len(rs), 4) if rs else None,
        })

    def _rank(row: dict):
        return (-(row["avgScore"] if row["avgScore"] is not None else -1.0),
                -(row["avgTokensPerSec"] or 0.0),
                row["avgLatencyMs"] if row["avgLatencyMs"] is not None else 1e18)

    rows.sort(key=_rank)
    for i, row in enumerate(rows):
        row["winner"] = i == 0
    return rows


def case_matrix(run_id: str) -> list[dict]:
    """Per case: each model's best output and score side by side (drill-down)."""
    results = list_results(run_id)
    by_case: dict[int, dict[str, list[dict]]] = {}
    for r in results:
        by_case.setdefault(r["itemIndex"], {}).setdefault(r["model"], []).append(r)
    out = []
    for idx in sorted(by_case):
        models = {}
        for model, rs in by_case[idx].items():
            scores = [r["score"] for r in rs if r["score"] is not None]
            best = max(rs, key=lambda r: (r["score"] if r["score"] is not None else -1, -(r["latencyMs"] or 0)))
            models[model] = {"score": _mean(scores), "output": best["output"], "error": best["error"],
                             "rationale": best["rationale"], "toolCalls": best["toolCalls"],
                             "latencyMs": best["latencyMs"], "reps": len(rs)}
        out.append({"itemIndex": idx, "models": models})
    return out
