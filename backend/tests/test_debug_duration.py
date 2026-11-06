"""Hermetic debug-run duration derivation tests."""

import importlib

import pytest


@pytest.fixture()
def ddb(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as debug_db
    return importlib.reload(debug_db)


def _run(ddb, run_id, start_time, end_time, **extra):
    run = {
        "run_id": run_id,
        "session_id": "channel:AG1:conv1",
        "start_time": start_time,
        "end_time": end_time,
        "provider": "local",
        "model": "local-qwen",
        **extra,
    }
    ddb.insert_run(run, [])


def _duration(ddb, run_id):
    conn = ddb.get_connection()
    try:
        return conn.execute(
            "SELECT duration_ms FROM debug_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["duration_ms"]
    finally:
        conn.close()


def test_duration_is_derived_when_omitted(ddb):
    _run(ddb, "derived", "2026-08-20T10:00:00.000Z", "2026-08-20T10:00:01.234Z")

    assert _duration(ddb, "derived") == 1234


def test_explicit_duration_is_preserved(ddb):
    _run(ddb, "explicit", "2026-08-20T10:00:00+00:00", "2026-08-20T10:00:01+00:00",
         duration_ms=7)

    assert _duration(ddb, "explicit") == 7


def test_unparseable_times_leave_duration_null(ddb):
    _run(ddb, "invalid", "not-a-time", "still-not-a-time")

    assert _duration(ddb, "invalid") is None
