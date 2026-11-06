"""Analytics aggregation (PRD-06) over a seeded temp debug DB."""

import importlib

import pytest

from gateway.pricing import estimate_cost

# The external call's cost is now computed authoritatively by insert_run from the
# shared price table (callers can no longer supply it). tokens=50 → 25 in / 25 out.
_EXTERNAL_MODEL = "claude-sonnet-5"
_EXTERNAL_COST = estimate_cost(_EXTERNAL_MODEL, 25, 25)


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    """Point the trace DB at a temp file; give fresh debug_db + analytics_db."""
    monkeypatch.setenv("VW_HOME", str(tmp_path))
    import config
    importlib.reload(config)
    import storage.debug_db as ddb
    importlib.reload(ddb)
    import storage.analytics_db as adb
    importlib.reload(adb)
    return ddb, adb


def _run(ddb, session_id, start_time, *, model="local-qwen", provider="local",
         endpoint="/v1/chat/completions", tokens=100, cost=0.0, is_error=0,
         tool_calls=0, duration_ms=200):
    run = {
        "run_id": f"{session_id}-{start_time}",
        "session_id": session_id,
        "start_time": start_time,
        "endpoint": endpoint,
        "provider": provider,
        "model": model,
        "status_code": 500 if is_error else 200,
        "is_error": is_error,
        "input_tokens": tokens // 2,
        "output_tokens": tokens - tokens // 2,
        "total_tokens": tokens,
        "cost": cost,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
        "created_at": start_time,
    }
    ddb.insert_run(run, [])


def _seed(ddb):
    # Agent A — local worker, two days, one error, some tool calls.
    _run(ddb, "worker:agentA", "2026-06-20T10:00:00+00:00", tokens=100, tool_calls=2)
    _run(ddb, "worker:agentA", "2026-06-20T11:00:00+00:00", tokens=200, is_error=1)
    _run(ddb, "worker:agentA", "2026-06-21T09:00:00+00:00", tokens=150, tool_calls=1)
    # A hosted/external call — anthropic, real (computed) cost.
    _run(ddb, "anthropic:claude", "2026-06-20T12:00:00+00:00",
         model=_EXTERNAL_MODEL, provider="anthropic", tokens=50)


def test_summary(mods):
    ddb, adb = mods
    _seed(ddb)
    s = adb.summary("2026-06-20", "2026-06-21")
    assert s["calls"] == 4
    assert s["totalTokens"] == 500
    assert s["errors"] == 1
    assert s["errorRate"] == round(1 / 4, 4)
    assert s["cost"] == _EXTERNAL_COST
    assert s["toolCalls"] == 3
    assert s["avgLatencyMs"] == 200


def test_summary_window_excludes_outside(mods):
    ddb, adb = mods
    _seed(ddb)
    s = adb.summary("2026-06-21", "2026-06-21")  # only the 21st
    assert s["calls"] == 1
    assert s["totalTokens"] == 150


def test_trends_buckets_by_day(mods):
    ddb, adb = mods
    _seed(ddb)
    series = adb.trends("2026-06-20", "2026-06-21", "day")
    by_day = {row["date"]: row for row in series}
    assert by_day["2026-06-20"]["calls"] == 3
    assert by_day["2026-06-20"]["totalTokens"] == 350
    assert by_day["2026-06-20"]["errors"] == 1
    assert by_day["2026-06-21"]["calls"] == 1


def test_breakdown_by_model(mods):
    ddb, adb = mods
    _seed(ddb)
    rows = {r["key"]: r for r in adb.breakdown("model", "2026-06-20", "2026-06-21")}
    assert rows["local-qwen"]["calls"] == 3
    assert rows[_EXTERNAL_MODEL]["calls"] == 1
    assert rows[_EXTERNAL_MODEL]["cost"] == _EXTERNAL_COST


def test_breakdown_by_provider(mods):
    ddb, adb = mods
    _seed(ddb)
    rows = {r["key"]: r for r in adb.breakdown("provider", "2026-06-20", "2026-06-21")}
    assert rows["local"]["calls"] == 3
    assert rows["anthropic"]["cost"] == _EXTERNAL_COST


def test_breakdown_bad_dimension(mods):
    _, adb = mods
    with pytest.raises(ValueError):
        adb.breakdown("nonsense")


def test_scorecard_attributes_worker_to_agent(mods):
    ddb, adb = mods
    _seed(ddb)
    cards = {c["agentId"]: c for c in adb.scorecard("2026-06-20", "2026-06-21")}
    a = cards["agentA"]
    assert a["calls"] == 3
    assert a["errors"] == 1
    assert a["errorRate"] == round(1 / 3, 4)
    assert a["toolCalls"] == 3
    assert a["sessions"] == 1
    assert a["activeDays"] == 2
    # The external anthropic call lands under the synthetic "external" card.
    assert None in cards  # agentId is None for external
    assert cards[None]["calls"] == 1


def test_scorecard_filter_single_agent(mods):
    ddb, adb = mods
    _seed(ddb)
    cards = adb.scorecard("2026-06-20", "2026-06-21", agent_id="agentA")
    assert len(cards) == 1
    assert cards[0]["agentId"] == "agentA"


def test_export_runs(mods):
    ddb, adb = mods
    _seed(ddb)
    rows = adb.export_runs("2026-06-20", "2026-06-21")
    assert len(rows) == 4
    worker_rows = [r for r in rows if r["agentId"] == "agentA"]
    assert len(worker_rows) == 3
    # Rows are ordered by start_time ascending.
    assert rows[0]["startTime"] <= rows[-1]["startTime"]
