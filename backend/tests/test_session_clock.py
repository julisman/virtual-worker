"""Session-pinned clock in the composed system prompt.

Agents used to answer time questions from training data because the prompt
carried no clock (kept out to protect the llama.cpp prefix cache). The fix is
one clock line pinned per session_id, refreshed every 15 minutes: real machine
time on a cold session, byte-identical prompts within a chat burst.
"""

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import pytest

import config
from agent import compose as compose_mod
from agent.compose import compose

BASE = datetime(2026, 7, 23, 14, 0, tzinfo=timezone(timedelta(hours=7)))


@pytest.fixture()
def clock(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(compose_mod, "_clock_cache", OrderedDict())
    state = {"now": BASE}
    monkeypatch.setattr(compose_mod, "_now_local", lambda: state["now"])
    return state


def _system(session_id):
    system, *_ = compose({"persona": "You help."}, session_id=session_id)
    return system


def test_cold_session_gets_machine_time(clock):
    system = _system("chat:s1")
    assert "Current date and time:" in system
    assert "Thursday, 2026-07-23 14:00 (UTC+0700)" in system


def test_pinned_within_refresh_window(clock):
    first = _system("chat:s1")
    clock["now"] = BASE + timedelta(minutes=5)
    # 5 minutes later, same session: byte-identical prompt (prefix-cache safe).
    assert _system("chat:s1") == first
    # …but a NEW session pins the fresh time immediately.
    assert "14:05" in _system("chat:s2")


def test_refreshes_after_window(clock):
    _system("chat:s1")
    clock["now"] = BASE + timedelta(minutes=16)
    assert "14:16" in _system("chat:s1")


def test_reused_headless_session_ids_stay_fresh(clock):
    # The scheduler/worker reuse one session id across runs that fire well
    # apart — each run must see current time, not the first run's pin.
    _system("schedule:sc_1")
    clock["now"] = BASE + timedelta(hours=3)
    assert "17:00" in _system("schedule:sc_1")


def test_cache_is_bounded(clock):
    for i in range(compose_mod._CLOCK_CACHE_MAX + 50):
        compose_mod.clock_line(f"s{i}")
    assert len(compose_mod._clock_cache) == compose_mod._CLOCK_CACHE_MAX


def test_clock_line_is_last_block(clock):
    # The clock is the ONLY volatile text in the prompt — it must sit at the
    # END so the semi-static blocks before it (persona, skills, memory, team,
    # board) stay prefix-cache reusable across cold sessions.
    system, *_ = compose({"persona": "You help."}, session_id="chat:s1",
                         extra_system="Be brief.")
    assert system.endswith(compose_mod.clock_line("chat:s1"))
