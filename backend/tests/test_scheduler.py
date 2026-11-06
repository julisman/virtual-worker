"""Scheduler — cron evaluation + schedule_store CRUD/due logic (no network)."""

import asyncio
from datetime import datetime

import pytest

import config
from agent import cron


# ─── cron matcher ────────────────────────────────────────────────────────────

def test_cron_matches_daily_time():
    assert cron.matches("0 17 * * *", datetime(2026, 6, 28, 17, 0))
    assert not cron.matches("0 17 * * *", datetime(2026, 6, 28, 17, 1))
    assert not cron.matches("0 17 * * *", datetime(2026, 6, 28, 16, 0))


def test_cron_step_and_range():
    assert cron.matches("*/15 * * * *", datetime(2026, 6, 28, 3, 30))
    assert not cron.matches("*/15 * * * *", datetime(2026, 6, 28, 3, 31))
    # 9am on weekdays — 2026-06-29 is a Monday, 2026-06-28 a Sunday
    assert cron.matches("0 9 * * 1-5", datetime(2026, 6, 29, 9, 0))
    assert not cron.matches("0 9 * * 1-5", datetime(2026, 6, 28, 9, 0))


def test_cron_day_of_week_sunday():
    # 2026-06-28 is a Sunday; crontab dow 0 and 7 both mean Sunday
    assert cron.matches("0 14 * * 0", datetime(2026, 6, 28, 14, 0))
    assert cron.matches("0 14 * * 7", datetime(2026, 6, 28, 14, 0))
    assert not cron.matches("0 14 * * 1", datetime(2026, 6, 28, 14, 0))


def test_cron_macros_and_validate():
    assert cron.validate("@daily")
    assert cron.matches("@daily", datetime(2026, 6, 28, 0, 0))
    assert not cron.validate("99 * * * *")
    assert not cron.validate("nonsense")
    assert not cron.validate("* * * *")  # only 4 fields


def test_cron_next_run():
    nxt = cron.next_run("0 14 * * *", datetime(2026, 6, 28, 15, 0))
    assert nxt == datetime(2026, 6, 29, 14, 0)


def test_cron_describe():
    assert cron.describe("0 17 * * *") == "Daily at 17:00"
    assert cron.describe("*/15 * * * *") == "Every 15 minutes"
    assert cron.describe("@daily") in ("Daily at 00:00", "@daily")


# ─── schedule_store ──────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from storage import schedule_store
    return schedule_store


def test_create_cron_computes_next(store):
    s = store.create("a1", "Daily", "summarize", kind="cron", cron_expr="0 14 * * *")
    assert s["kind"] == "cron"
    assert s["cronText"] == "Daily at 14:00"
    assert s["nextRunAt"] is not None and s["nextRunAt"].endswith("14:00:00")
    assert s["enabled"] is True


def test_create_invalid_cron_rejected(store):
    with pytest.raises(ValueError):
        store.create("a1", "bad", "x", kind="cron", cron_expr="99 * * * *")


def test_one_time_due_and_disables_after_run(store):
    s = store.create("a1", "Once", "say hi", kind="at", run_at="2020-01-01T09:00")
    due_ids = [d["id"] for d in store.list_due()]
    assert s["id"] in due_ids  # a past one-time fires once (catch-up)
    store.record_run(s["id"], "ok", result="hello")
    after = store.get(s["id"])
    assert after["enabled"] is False
    assert after["nextRunAt"] is None
    assert after["lastStatus"] == "ok"
    assert after["runCount"] == 1


def test_future_cron_not_due(store):
    s = store.create("a1", "Future", "x", kind="cron", cron_expr="0 3 * * *")
    # next run is tomorrow 03:00 — should not be due right now
    assert s["id"] not in [d["id"] for d in store.list_due()]


def test_update_toggle_and_scope(store):
    s = store.create("a1", "S", "x", kind="cron", cron_expr="0 9 * * *")
    store.create("a2", "other", "y", kind="cron", cron_expr="0 9 * * *")
    assert len(store.list_for_agent("a1")) == 1  # scoped per agent
    off = store.update(s["id"], enabled=False)
    assert off["enabled"] is False and off["nextRunAt"] is None
    assert store.delete(s["id"]) is True
    assert store.get(s["id"]) is None


def test_record_run_advances_recurring(store):
    s = store.create("a1", "R", "x", kind="cron", cron_expr="0 14 * * *")
    first_next = s["nextRunAt"]
    store.record_run(s["id"], "ok", result="done")
    after = store.get(s["id"])
    assert after["enabled"] is True             # recurring stays on
    assert after["runCount"] == 1
    assert after["nextRunAt"] is not None       # advanced to the next occurrence
    assert after["nextRunAt"] >= first_next


def test_schedule_tools_report_now(store):
    # The prompt carries no clock at all (a per-turn injection confused small
    # local models, and a persona clock breaks the cached prefix). The agent
    # learns "now" from the schedule tools' own results instead.
    from datetime import datetime

    from agent.scheduler import make_schedule_tools
    tools = make_schedule_tools("a-now")
    today = datetime.now().strftime("%Y-%m-%d")
    listed = asyncio.run(tools["schedule_list"]["fn"]())          # empty case
    assert today in listed
    asyncio.run(tools["schedule_create"]["fn"](prompt="p", cron="0 8 * * *"))
    listed = asyncio.run(tools["schedule_list"]["fn"]())          # non-empty case
    assert today in listed
    missing = asyncio.run(tools["schedule_create"]["fn"](prompt="p"))  # no cron/at
    assert today in missing
    bad = asyncio.run(tools["schedule_create"]["fn"](prompt="p", at="not-a-date"))
    assert today in bad


def test_compose_persona_is_stable_and_time_free(store):
    # The persona system prompt must NOT carry the volatile timestamp: keeping it
    # out is what lets llama.cpp reuse the prefix KV-cache across turns. It must
    # also be byte-identical on repeated composes (no per-call volatility).
    from agent.compose import compose
    system_a, registry, *_ = compose({"id": "a1", "persona": "You are helpful."})
    system_b, *_ = compose({"id": "a1", "persona": "You are helpful."})
    assert "Current date & time:" not in system_a
    assert system_a == system_b            # stable prefix across turns
    assert "schedule_create" in registry   # scheduler tools still wired


def test_schedule_tools(store):
    from agent.scheduler import make_schedule_tools
    tools = make_schedule_tools("a1")
    assert set(tools) == {"schedule_create", "schedule_list", "schedule_delete"}
    out = asyncio.run(tools["schedule_create"]["fn"](title="t", prompt="do it", cron="0 8 * * *"))
    assert "scheduled" in out.lower()
    listed = asyncio.run(tools["schedule_list"]["fn"]())
    assert "Daily at 08:00" in listed
    # missing schedule + missing prompt are handled gracefully
    assert "needs" in asyncio.run(tools["schedule_create"]["fn"](prompt=""))
    assert "no schedule" in asyncio.run(tools["schedule_delete"]["fn"](id="nope"))
