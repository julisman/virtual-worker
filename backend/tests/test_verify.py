"""Verify pillar — the worker self-checks a 'done' task before accepting it."""

import asyncio

import pytest

import config
from agent import verify
from agent import worker as worker_mod
from storage import activity_store, agent_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    return tmp_path


def _patch_verdict(monkeypatch, text):
    async def fake_complete(model, system, user, provider=None):
        return text
    monkeypatch.setattr(verify, "_complete", fake_complete)


# --- verify_task parsing ---------------------------------------------------- #

def test_verify_parses_verified(monkeypatch):
    _patch_verdict(monkeypatch, "VERIFIED: the README exists and covers setup")
    ok, reason = asyncio.run(verify.verify_task("m", {"title": "x", "resultSummary": "done"}))
    assert ok is True and "README" in reason


def test_verify_parses_needs_work(monkeypatch):
    _patch_verdict(monkeypatch, "NEEDS_WORK: no tests were added")
    ok, reason = asyncio.run(verify.verify_task("m", {"title": "x"}))
    assert ok is False and "tests" in reason


def test_verify_defaults_true_on_error(monkeypatch):
    async def boom(*a):
        raise RuntimeError("model down")
    monkeypatch.setattr(verify, "_complete", boom)
    ok, _ = asyncio.run(verify.verify_task("m", {"title": "x"}))
    assert ok is True  # a flaky reviewer never blocks real work


# --- worker integration ----------------------------------------------------- #

def test_worker_keeps_done_when_verified(db, monkeypatch):
    a = agent_store.create_agent({"name": "H", "role": "Intern"})
    t = task_store.create(a["id"], {"title": "ship", "status": "done"})
    _patch_verdict(monkeypatch, "VERIFIED: looks complete")
    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._verify(a["id"], "m", task_store.get(a["id"], t["id"])))
    assert task_store.get(a["id"], t["id"])["status"] == "done"
    assert any(act["type"] == "verify" for act in activity_store.list_for(a["id"]))


def test_worker_sends_back_when_needs_work(db, monkeypatch):
    a = agent_store.create_agent({"name": "H", "role": "Intern"})
    t = task_store.create(a["id"], {"title": "ship", "status": "done"})
    _patch_verdict(monkeypatch, "NEEDS_WORK: missing error handling")
    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._verify(a["id"], "m", task_store.get(a["id"], t["id"])))
    back = task_store.get(a["id"], t["id"])
    assert back["status"] == "todo"
    assert verify.attempts(back) == 1


def test_verify_attempts_capped(db, monkeypatch):
    a = agent_store.create_agent({"name": "H", "role": "Intern"})
    t = task_store.create(a["id"], {"title": "ship", "status": "done",
                                    "metadata": {"verifyAttempts": verify.MAX_VERIFY_ATTEMPTS}})
    _patch_verdict(monkeypatch, "NEEDS_WORK: still bad")  # would send back, but cap reached
    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._verify(a["id"], "m", task_store.get(a["id"], t["id"])))
    # Cap reached -> accept done instead of ping-ponging forever.
    assert task_store.get(a["id"], t["id"])["status"] == "done"


def test_verify_finds_verdict_in_chatty_reply(monkeypatch):
    # Chatty local models often preamble before the verdict line — the parser
    # must find NEEDS_WORK on a later line, not read line 1 as a pass.
    _patch_verdict(monkeypatch, "Let me review.\nThe work lacks coverage.\n"
                                "NEEDS_WORK: no tests were added")
    ok, reason = asyncio.run(verify.verify_task("m", {"title": "x"}))
    assert ok is False and "tests" in reason


def test_verify_unparseable_reply_is_inconclusive_not_a_verdict(monkeypatch):
    # No VERIFIED/NEEDS_WORK anywhere: fail open (like an unavailable reviewer)
    # but say so, instead of counting arbitrary text as a pass verdict.
    _patch_verdict(monkeypatch, "Sure! The task looks plausibly fine to me overall.")
    ok, reason = asyncio.run(verify.verify_task("m", {"title": "x"}))
    assert ok is True and "inconclusive" in reason
