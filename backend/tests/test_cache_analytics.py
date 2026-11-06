"""Hermetic cache-miss diagnostics derived from Debug traces."""

import importlib

import config


def _run(ddb, session, run_id, start, *, read, prompt, model="gpt-4o",
         provider="openai", tools=None, request_flags=True):
    usage = {"input": prompt, "output": 10, "totalTokens": prompt + 10,
             "cacheRead": read, "cacheWrite": 0, "cost": 0}
    ddb.insert_run({
        "run_id": run_id, "session_id": session, "start_time": start,
        "end_time": start, "provider": provider, "model": model,
        "status_code": 200, "stream": True, "input_tokens": prompt,
        "output_tokens": 10, "cache_read": read, "cache_write": 0,
        "total_tokens": prompt + 10, "request_json":
            '{"prompt_cache_key":"ves-key"}' if request_flags else None,
    }, [{"type": "assistant", "text": "ok", "model": model,
         "provider": provider, "usage": usage}]
    + [{"type": "tool_call", "tool_name": name} for name in (tools or [])])


def test_cache_miss_is_annotated_with_rebill_and_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as ddb
    ddb = importlib.reload(ddb)
    _run(ddb, "s", "one", "2026-08-21T10:00:00+00:00", read=80, prompt=100)
    _run(ddb, "s", "two", "2026-08-21T10:01:00+00:00", read=0, prompt=120)

    data = ddb.get_session("s")
    miss = data["runs"][1]["cacheMiss"]
    assert miss["rebilledTokens"] == 120
    assert miss["cause"] == "cache prefix changed"
    assert data["runs"][1]["entries"][0]["cacheMiss"] == miss
    # estimatedCost reuses the middleware price table (single source of truth):
    # the input-priced rebill of the same model/tokens.
    from middleware.api_debug import _cost
    assert miss["estimatedCost"] == _cost("gpt-4o", 120, 0)
    assert miss["estimatedCost"] > 0


def test_first_turn_is_not_a_warning_and_idle_gap_is_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as ddb
    ddb = importlib.reload(ddb)
    _run(ddb, "s", "one", "2026-08-21T10:00:00+00:00", read=0, prompt=100,
         provider="anthropic", model="claude-sonnet-4-6")
    _run(ddb, "s", "two", "2026-08-21T10:07:00+00:00", read=0, prompt=120,
         provider="anthropic", model="claude-sonnet-4-6")
    data = ddb.get_session("s")
    assert "cacheMiss" not in data["runs"][0]
    assert data["runs"][1]["cacheMiss"]["cause"] == "idle gap > 5m TTL"
