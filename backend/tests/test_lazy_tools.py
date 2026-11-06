"""Lazy tool exposure: big ``deferred`` tool sets (all MCP tools) are hidden
behind a ``search_tools`` meta-tool so the per-turn prompt stays small, and get
unlocked on demand. See agent/loop.py (SEARCH_TOOLS_NAME / lazy machinery)."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    monkeypatch.setattr(loop, "_SESSION_UNLOCKS", {})
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _big_schema(name):
    # A schema chunky enough that a handful clear LAZY_TOOL_THRESHOLD_CHARS.
    props = {f"p{i}": {"type": "string", "description": "d" * 300} for i in range(12)}
    return {"type": "function", "function": {
        "name": name, "description": f"{name} operation " * 20,
        "parameters": {"type": "object", "properties": props, "required": ["p0"]}}}


def _mcp_registry(n=20):
    async def _noop(**kw):
        return "ok"
    reg = {}
    reg["task_list"] = {"fn": _noop, "schema": {"type": "function", "function": {
        "name": "task_list", "parameters": {"type": "object", "properties": {}}}}}
    for i in range(n):
        name = f"mcp__jira__create_issue_{i}"
        reg[name] = {"fn": _noop, "schema": _big_schema(name), "deferred": True}
    return reg


def _scripted_stream(turns):
    """Record each turn's payload; emit the scripted tool-call / answer for it."""
    seen = {"payloads": [], "n": 0}

    async def stream(payload, provider=None):
        i = seen["n"]
        seen["n"] += 1
        seen["payloads"].append(payload)
        step = turns[i] if i < len(turns) else None
        if step:  # a tool call
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": f"c{i}", "type": "function",
                "function": {"name": step[0], "arguments": json.dumps(step[1])}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:  # final answer
            yield _chunk(delta={"content": "done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return stream, seen


def _run(registry, **kw):
    async def collect():
        out = []
        async for ev in loop.run([{"role": "user", "content": "hi"}],
                                  model="local", tools=registry, **kw):
            out.append(ev)
        return out
    return asyncio.run(collect())


def _tool_names(payload):
    return {t["function"]["name"] for t in payload.get("tools", [])}


def test_deferred_tools_hidden_behind_search_tools(home, monkeypatch):
    reg = _mcp_registry()
    stream, seen = _scripted_stream([None])  # model answers immediately
    monkeypatch.setattr(loop.egress, "stream", stream)
    _run(reg)
    names = _tool_names(seen["payloads"][0])
    assert loop.SEARCH_TOOLS_NAME in names          # meta-tool injected
    assert "task_list" in names                     # core tool still visible
    assert not any(n.startswith("mcp__") for n in names)  # MCP tools hidden


def test_search_tools_unlocks_matching_tool(home, monkeypatch):
    reg = _mcp_registry()
    # turn 0: model searches; turn 1: model calls the unlocked tool; turn 2: answer
    turns = [(loop.SEARCH_TOOLS_NAME, {"query": "create issue 7"}),
             ("mcp__jira__create_issue_7", {"p0": "x"}),
             None]
    stream, seen = _scripted_stream(turns)
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = _run(reg)

    first = _tool_names(seen["payloads"][0])
    after = _tool_names(seen["payloads"][1])
    assert "mcp__jira__create_issue_7" not in first     # hidden before search
    assert "mcp__jira__create_issue_7" in after         # unlocked after search
    # the actual MCP tool ran (its result flows back), and the run completed
    assert any(e.get("type") == "tool_result"
               and e.get("name") == "mcp__jira__create_issue_7" for e in events)
    assert any(e.get("type") == "done" for e in events)


def test_small_deferred_set_not_hidden(home, monkeypatch):
    # A single tiny deferred tool is below the threshold — show it, don't add a
    # needless discovery round-trip.
    async def _noop(**kw):
        return "ok"
    reg = {"mcp__x__ping": {"fn": _noop, "deferred": True, "schema": {
        "type": "function", "function": {"name": "mcp__x__ping",
        "parameters": {"type": "object", "properties": {}}}}}}
    stream, seen = _scripted_stream([None])
    monkeypatch.setattr(loop.egress, "stream", stream)
    _run(reg)
    names = _tool_names(seen["payloads"][0])
    assert "mcp__x__ping" in names                    # shown directly
    assert loop.SEARCH_TOOLS_NAME not in names         # no meta-tool needed


def test_search_tools_browse_fallback_on_no_match(home):
    reg = _mcp_registry()
    deferred = {n for n in reg if reg[n].get("deferred")}
    active = {n for n in reg if n not in deferred}
    fn = loop._make_search_tools(reg, deferred, active)
    res = asyncio.run(fn(query="zzz-nothing-matches"))
    assert "No tool matched" in res
    assert "mcp__jira__create_issue_0" in res          # lists what's available
    # a miss must not unlock anything
    assert not any(n.startswith("mcp__") for n in active)


def test_unlocks_persist_across_runs_in_same_session(home, monkeypatch):
    # Turn 1 unlocks a tool via search_tools; turn 2 (same session) must see it
    # directly — no second discovery round-trip.
    reg = _mcp_registry()
    # query "7" matches only create_issue_7 / create_issue_17 — a small unlock set
    turns = [(loop.SEARCH_TOOLS_NAME, {"query": "7"}),
             ("mcp__jira__create_issue_7", {"p0": "x"}),
             None]
    stream, _ = _scripted_stream(turns)
    monkeypatch.setattr(loop.egress, "stream", stream)
    _run(reg, session_id="chat:s1")

    stream2, seen2 = _scripted_stream([None])
    monkeypatch.setattr(loop.egress, "stream", stream2)
    _run(_mcp_registry(), session_id="chat:s1")
    names = _tool_names(seen2["payloads"][0])
    assert "mcp__jira__create_issue_7" in names        # unlocked last turn
    assert "mcp__jira__create_issue_0" not in names    # never unlocked — hidden
    assert loop.SEARCH_TOOLS_NAME in names             # discovery still offered


def test_unlocks_do_not_leak_across_sessions(home, monkeypatch):
    reg = _mcp_registry()
    turns = [(loop.SEARCH_TOOLS_NAME, {"query": "create issue 7"}),
             ("mcp__jira__create_issue_7", {"p0": "x"}),
             None]
    stream, _ = _scripted_stream(turns)
    monkeypatch.setattr(loop.egress, "stream", stream)
    _run(reg, session_id="chat:s1")

    stream2, seen2 = _scripted_stream([None])
    monkeypatch.setattr(loop.egress, "stream", stream2)
    _run(_mcp_registry(), session_id="chat:s2")
    names = _tool_names(seen2["payloads"][0])
    assert not any(n.startswith("mcp__") for n in names)   # other session: still hidden


def test_lazy_tools_off_exposes_everything_directly(home, monkeypatch):
    # Guardrail ``lazyTools: "off"`` (loop param lazy_tools=False): every MCP
    # schema is sent to the model directly and no search_tools hop exists —
    # for small local models that announce instead of making the extra call.
    reg = _mcp_registry(n=20)
    stream, seen = _scripted_stream([None])  # model answers immediately
    monkeypatch.setattr(loop.egress, "stream", stream)
    _run(reg, lazy_tools=False)
    names = _tool_names(seen["payloads"][0])
    assert loop.SEARCH_TOOLS_NAME not in names
    assert "mcp__jira__create_issue_0" in names
    assert len(names) == 21                     # 20 MCP + task_list, nothing hidden


def test_lazy_policy_is_model_aware():
    # "auto" (default): direct on the local model, lazy on a remote provider.
    from agent.runner import resolve_lazy_tools
    remote = {"provider": "anthropic"}
    assert resolve_lazy_tools({}, None) is False                     # local → show all
    assert resolve_lazy_tools({}, remote) is True                    # paid  → lazy
    assert resolve_lazy_tools(None, remote) is True
    assert resolve_lazy_tools({"lazyTools": "off"}, remote) is False  # explicit wins
    assert resolve_lazy_tools({"lazyTools": "on"}, None) is True
