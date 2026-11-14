"""Debug context-window meter: peak prompt / window / per-call series round-trip."""

import config
import pytest

from storage import debug_db


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _run(run_id, session_id, **kw):
    base = {"run_id": run_id, "session_id": session_id, "start_time": "2026-11-14T08:00:00",
            "end_time": "2026-11-14T08:01:00", "model": "m", "provider": "local",
            "input_tokens": 400, "output_tokens": 10}
    base.update(kw)
    return base


def test_context_fields_round_trip(home):
    debug_db.insert_run(_run("r1", "schedule:s1", peak_context_tokens=900,
                             context_window=2000, context_series=[300, 900, 400]), [])
    debug_db.insert_run(_run("r2", "schedule:s1", start_time="2026-11-14T09:00:00",
                             peak_context_tokens=500, context_window=2000,
                             context_series=[500]), [])
    run = debug_db.get_session("schedule:s1")["runs"][0]
    assert run["peakContextTokens"] == 900
    assert run["contextWindow"] == 2000
    assert run["contextSeries"] == [300, 900, 400]
    sess = next(s for s in debug_db.list_sessions() if s["sessionId"] == "schedule:s1")
    assert sess["peakContextTokens"] == 900         # biggest across the session
    assert sess["lastContextTokens"] == 500         # the latest run's peak
    assert sess["contextWindow"] == 2000


def test_single_call_capture_uses_input_as_context(home):
    """A gateway (middleware) capture has no series: its prompt IS the context."""
    debug_db.insert_run(_run("g1", "anthropic:m"), [])
    run = debug_db.get_session("anthropic:m")["runs"][0]
    assert run["peakContextTokens"] == 400
    assert run["contextSeries"] == []
    assert run["contextWindow"] is None
