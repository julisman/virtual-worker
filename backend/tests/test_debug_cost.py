"""insert_run computes cost authoritatively from the shared price table.

The debug store is THE single place cost is computed — always locally from the
price table, never trusted from callers. Agent-loop runs (/api/chat) previously
stored 0 because the loop never computed a cost; these tests pin the contract.
"""

import importlib

import config
from gateway import pricing


def _fresh_ddb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as ddb
    return importlib.reload(ddb)


def _stored_cost(ddb, session_id, run_id):
    data = ddb.get_session(session_id)
    run = next(r for r in data["runs"] if r["runId"] == run_id)
    return run["totalCost"]


def test_agent_loop_run_gets_nonzero_cost_from_prompt_sum(tmp_path, monkeypatch):
    # Pin the DeepSeek clock so the expected value is deterministic.
    monkeypatch.setattr(pricing, "deepseek_is_peak", lambda now: True)
    ddb = _fresh_ddb(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "APP_HOME", tmp_path)  # survive the reload

    model = "llm-gateway--deepseek-v4-flash"
    ddb.insert_run({
        "run_id": "r1", "session_id": "s", "start_time": "2026-08-24T02:00:00+00:00",
        "provider": "openai-compatible", "model": model, "status_code": 200,
        "input_tokens": 4876, "output_tokens": 115, "prompt_tokens_sum": 4876,
        "total_tokens": 4991,
    }, [])

    expected = pricing.estimate_cost(model, 4876, 115)
    assert expected > 0
    assert _stored_cost(ddb, "s", "r1") == expected


def test_bogus_incoming_cost_is_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing, "deepseek_is_peak", lambda now: True)
    ddb = _fresh_ddb(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "APP_HOME", tmp_path)

    model = "llm-gateway--deepseek-v4-flash"
    ddb.insert_run({
        "run_id": "r1", "session_id": "s", "start_time": "2026-08-24T02:00:00+00:00",
        "provider": "openai-compatible", "model": model, "status_code": 200,
        "input_tokens": 4876, "output_tokens": 115, "prompt_tokens_sum": 4876,
        "cost": 999.0,
    }, [])

    stored = _stored_cost(ddb, "s", "r1")
    assert stored != 999.0
    assert stored == pricing.estimate_cost(model, 4876, 115)


def test_unknown_model_costs_zero(tmp_path, monkeypatch):
    ddb = _fresh_ddb(tmp_path, monkeypatch)

    ddb.insert_run({
        "run_id": "r1", "session_id": "s", "start_time": "2026-08-24T02:00:00+00:00",
        "provider": "local", "model": "LocalFT-Q35-v3", "status_code": 200,
        "input_tokens": 5000, "output_tokens": 200, "prompt_tokens_sum": 5000,
    }, [])

    assert _stored_cost(ddb, "s", "r1") == 0.0


def test_middleware_style_run_uses_input_tokens_fallback(tmp_path, monkeypatch):
    # Middleware single-call runs carry no/zero prompt_tokens_sum; cost must fall
    # back to input_tokens rather than billing 0 input.
    ddb = _fresh_ddb(tmp_path, monkeypatch)

    model = "gpt-4o"
    ddb.insert_run({
        "run_id": "r1", "session_id": "s", "start_time": "2026-08-24T02:00:00+00:00",
        "provider": "openai", "model": model, "status_code": 200,
        "input_tokens": 1000, "output_tokens": 500,  # no prompt_tokens_sum key
    }, [])

    expected = pricing.estimate_cost(model, 1000, 500)
    assert expected > 0
    assert _stored_cost(ddb, "s", "r1") == expected
