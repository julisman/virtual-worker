"""Harness review batch 2 — regression tests.

Covers: the context budget being enforceable even when a protected recent
message is huge; obsidian delivery confined to the configured vault; the CCR
store refreshing content on a hash conflict (16-hex hashes); run_now honoring
the in-flight guard in both the scheduler and the trigger engine; and a broken
MCP session marking itself dead so the pool respawns it."""

import asyncio

import pytest

import config
from agent import crusher, delivery, loop
from agent.scheduler import SchedulerManager
from agent.triggers import TriggerManager
from storage import ccr_store, settings


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# ─── context budget (loop._fit_context second pass) ─────────────────────────

def test_fit_context_shrinks_protected_recent_tool_results():
    convo = [
        {"role": "system", "content": "s" * 2000},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "mcp__x__y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "X" * 40000},  # protected (recent)
        {"role": "assistant", "content": "a" * 500},
        {"role": "user", "content": "follow-up"},
    ]
    out = loop._fit_context(convo, 10000)
    total = sum(loop._msg_chars(m) for m in out)
    assert total <= 10000                            # budget actually holds now
    assert out[0]["content"] == "s" * 2000           # system prompt untouched
    assert out[-1]["content"] == "follow-up"         # final message untouched


def test_fit_context_untouched_when_under_budget():
    convo = [{"role": "user", "content": "hi"}]
    assert loop._fit_context(convo, 1000) == convo


# ─── obsidian delivery confinement ───────────────────────────────────────────

def test_obsidian_refused_without_configured_vault(home):
    rec = asyncio.run(delivery.deliver("obsidian", str(home / "anywhere"), "text"))
    assert rec["status"] == "failed"
    assert "no Obsidian vault configured" in rec["error"]


def test_obsidian_refuses_target_outside_the_vault(home, tmp_path):
    settings.set("obsidianVault", str(tmp_path / "vault"))
    rec = asyncio.run(delivery.deliver("obsidian", str(tmp_path / "elsewhere"), "text"))
    assert rec["status"] == "failed"
    assert "inside it" in rec["error"]
    assert not (tmp_path / "elsewhere").exists()     # nothing was written


def test_obsidian_allows_subfolder_of_the_vault(home, tmp_path):
    vault = tmp_path / "vault"
    settings.set("obsidianVault", str(vault))
    rec = asyncio.run(delivery.deliver("obsidian", str(vault / "projects"), "body",
                                       subject="Report"))
    assert rec["status"] == "sent"
    assert list((vault / "projects").rglob("*.md"))


# ─── CCR store: content refresh on conflict + 16-hex hash ────────────────────

def test_ccr_put_refreshes_content_on_same_hash(home):
    ccr_store.put("a" * 16, "first content", "tool_a")
    ccr_store.put("a" * 16, "second content", "tool_b")
    row = ccr_store.get("a" * 16)
    assert row["content"] == "second content"        # not the stale first write
    assert row["toolName"] == "tool_b"


def test_content_hash_is_16_hex():
    h = crusher.content_hash("payload")
    assert len(h) == 16 and int(h, 16) >= 0


def test_retrieve_accepts_legacy_8_hex(home):
    tool = crusher.make_retrieve_tool()["retrieve_full"]["fn"]
    assert "no stored original" in asyncio.run(tool(hash="deadbeef"))  # valid, unknown
    assert "needs the hex hash" in asyncio.run(tool(hash="nope"))      # invalid format


# ─── run_now honors the in-flight guard ──────────────────────────────────────

def _fire_recorder(mgr):
    calls = []

    async def fake_fire(*a, **k):
        calls.append(a)
    mgr._fire = fake_fire
    return calls


def test_scheduler_run_now_skips_when_inflight(home):
    mgr = SchedulerManager()
    calls = _fire_recorder(mgr)
    mgr._inflight.add("s1")
    asyncio.run(mgr.run_now({"id": "s1"}))
    assert calls == []                               # no duplicate concurrent fire
    mgr._inflight.clear()
    asyncio.run(mgr.run_now({"id": "s1"}))
    assert len(calls) == 1                           # fires normally when idle


def test_trigger_run_now_skips_when_inflight(home):
    mgr = TriggerManager()
    calls = _fire_recorder(mgr)
    mgr._inflight.add("t1")
    asyncio.run(mgr.run_now({"id": "t1"}))
    assert calls == []
    mgr._inflight.clear()
    asyncio.run(mgr.run_now({"id": "t1"}))
    assert len(calls) == 1


# ─── MCP self-heal: a transport failure marks the pooled session dead ────────

def test_broken_mcp_session_marks_entry_dead():
    from agent import mcp_client

    class FakeSession:
        async def call_tool(self, name, args):
            raise RuntimeError("broken pipe")

    class FakeEntry:
        session = FakeSession()
        error = None

    entry = FakeEntry()
    fn = mcp_client._make_fn(entry, "do_thing", "srv")
    out = asyncio.run(fn(x=1))
    assert "error" in out
    assert entry.error is not None                   # alive() will now be False


def test_protocol_error_keeps_mcp_session_alive():
    from agent import mcp_client

    exc = mcp_client.McpError.__new__(mcp_client.McpError)  # no ctor args needed

    class FakeSession:
        async def call_tool(self, name, args):
            raise exc

    class FakeEntry:
        session = FakeSession()
        error = None

    entry = FakeEntry()
    fn = mcp_client._make_fn(entry, "do_thing", "srv")
    out = asyncio.run(fn())
    assert "error" in out
    assert entry.error is None                       # JSON-RPC error ≠ dead transport
