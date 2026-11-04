# Cost & Usage Analytics (PRD-06) — read API over the captured debug facts.

"""HTTP surface for the analytics layer (``/api/analytics``).

  GET  /api/analytics/summary    -> top KPI tiles for the window
  GET  /api/analytics/trends     -> per-day/week time series
  GET  /api/analytics/breakdown  -> aggregate table by agent|model|provider|endpoint|session
  GET  /api/analytics/scorecard  -> per-agent virtual worker scorecards
  GET  /api/analytics/export     -> CSV/JSON of runs in the window
  GET  /api/analytics/budget     -> read budget config + current consumption
  PUT  /api/analytics/budget     -> set the (optional) cost / error-rate budget

Everything reads the local ``app.db`` (no external calls). The budget is a
single optional threshold config stored in ``settings.json``; the summary and
budget responses flag when consumption exceeds it. Read-only over the debug
tables — ``ApiDebugMiddleware`` stays the single capture path.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from storage import analytics_db, settings

logger = logging.getLogger(__name__)
router = APIRouter()

_BUDGET_KEY = "analyticsBudget"
_DEFAULT_BUDGET = {"enabled": False, "costUsd": 20.0, "errorRatePct": 10.0}


# --------------------------------------------------------------------------- #
# Budget config (optional threshold stored in settings.json)
# --------------------------------------------------------------------------- #

def _load_budget() -> dict:
    raw = settings.get(_BUDGET_KEY) or {}
    return {**_DEFAULT_BUDGET, **(raw if isinstance(raw, dict) else {})}


def _budget_status(summary: dict, budget: dict) -> dict:
    """Compare a window summary against the budget; flag breaches."""
    cost = summary.get("cost", 0.0)
    err_pct = summary.get("errorRate", 0.0) * 100
    cost_cap = float(budget.get("costUsd") or 0)
    err_cap = float(budget.get("errorRatePct") or 0)
    cost_over = bool(budget.get("enabled") and cost_cap > 0 and cost > cost_cap)
    err_over = bool(budget.get("enabled") and err_cap > 0 and err_pct > err_cap)
    return {
        "enabled": bool(budget.get("enabled")),
        "costUsd": cost_cap,
        "errorRatePct": err_cap,
        "spentUsd": round(cost, 6),
        "spentPct": round(cost / cost_cap * 100, 1) if cost_cap > 0 else None,
        "errorRateNow": round(err_pct, 2),
        "costExceeded": cost_over,
        "errorRateExceeded": err_over,
        "exceeded": cost_over or err_over,
    }


def _filters(provider: Optional[str], model: Optional[str], endpoint: Optional[str]) -> dict:
    return {"provider": provider, "model": model, "endpoint": endpoint}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get("/summary")
async def get_summary(
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
):
    s = analytics_db.summary(frm, to, _filters(provider, model, endpoint))
    s["budget"] = _budget_status(s, _load_budget())
    return s


@router.get("/trends")
async def get_trends(
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    bucket: str = "day",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
):
    return {"bucket": bucket, "series": analytics_db.trends(
        frm, to, bucket, _filters(provider, model, endpoint))}


@router.get("/breakdown")
async def get_breakdown(
    dimension: str = "model",
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
):
    try:
        rows = analytics_db.breakdown(dimension, frm, to, _filters(provider, model, endpoint))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"dimension": dimension, "rows": rows}


@router.get("/scorecard")
async def get_scorecard(
    agent_id: Optional[str] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
):
    return {"scorecards": analytics_db.scorecard(frm, to, agent_id)}


@router.get("/export")
async def export(
    format: str = "csv",
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
):
    rows = analytics_db.export_runs(frm, to, _filters(provider, model, endpoint))
    if format == "json":
        return JSONResponse(rows)
    # CSV (default).
    cols = ["runId", "sessionId", "agentId", "startTime", "endpoint", "provider",
            "model", "statusCode", "isError", "inputTokens", "outputTokens",
            "totalTokens", "cost", "toolCalls", "durationMs"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=virtual-worker-analytics.csv"},
    )


@router.get("/budget")
async def get_budget(
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
):
    s = analytics_db.summary(frm, to)
    return _budget_status(s, _load_budget())


@router.put("/budget")
async def put_budget(body: dict):
    budget = {
        "enabled": bool(body.get("enabled")),
        "costUsd": float(body.get("costUsd") or 0),
        "errorRatePct": float(body.get("errorRatePct") or 0),
    }
    settings.set(_BUDGET_KEY, budget)
    return budget
