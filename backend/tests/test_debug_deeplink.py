"""Deep-linkable traces: the loop surfaces a run id, stamps it on activity rows,
and a single run resolves back to its session (chat/worker → Debug deep-link)."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from storage import activity_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    # Force the guarded debug schema to be (re)created in this tmp db.
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _fake_stream(tool: bool):
    """One scripted turn: a tool call then an answer (tool=True), else just answer."""
    calls = {"n": 0}

    async def stream(payload, provider=None):
        turn = calls["n"]
        calls["n"] += 1
        if tool and turn == 0:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "ping", "arguments": json.dumps({"query": "hi"})},
            }]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return stream


def _ping_registry():
    async def ping(query: str = "") -> str:
        return "pong"
    return {"ping": {"fn": ping, "schema": {"type": "function", "function": {
        "name": "ping", "parameters": {"type": "object",
                                       "properties": {"query": {"type": "string"}}}}}}}


def _run(**kw):
    async def collect():
        events = []
        async for ev in loop.run([{"role": "user", "content": "hi"}], model="local", **kw):
            events.append(ev)
        return events
    return asyncio.run(collect())


def test_done_event_carries_run_id_matching_persisted_run(home, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", _fake_stream(tool=False))
    events = _run(session_id="chat:s1", agent_id="a1")
    done = next(e for e in events if e["type"] == "done")
    run_id = done["runId"]
    assert run_id and done["sessionId"] == "chat:s1"
    # The event's run id is exactly the id the trail was persisted under.
    got = debug_db.get_run(run_id)
    assert got is not None
    assert got["sessionId"] == "chat:s1"
    assert got["run"]["runId"] == run_id


def test_tool_call_activity_stamped_with_trace(home, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", _fake_stream(tool=True))
    events = _run(tools=_ping_registry(), session_id="worker:a1", agent_id="a1", task_id="t1")
    run_id = next(e for e in events if e["type"] == "done")["runId"]
    acts = activity_store.list_for("a1", task_id="t1")
    tc = next(a for a in acts if a["type"] == "tool_call")
    # The activity row deep-links to the exact run it came from.
    assert tc["detail"]["debugRunId"] == run_id
    assert tc["detail"]["debugSessionId"] == "worker:a1"
    assert tc["detail"]["tool"] == "ping"
    # And that run really exists in the trace store.
    assert debug_db.get_run(run_id)["run"]["toolCalls"] == 1


def test_error_event_carries_run_id(home, monkeypatch):
    async def boom(payload, provider=None):
        raise loop.UpstreamError(502, "upstream exploded")
        yield  # pragma: no cover - make it an async generator
    monkeypatch.setattr(loop.egress, "stream", boom)
    events = _run(session_id="chat:err", agent_id="a1")
    err = next(e for e in events if e["type"] == "error")
    assert err["runId"] and err["sessionId"] == "chat:err"
    # Errored run is still persisted and resolvable by id.
    got = debug_db.get_run(err["runId"])
    assert got and got["run"]["errorCount"] == 1


def test_get_run_none_for_unknown(home):
    assert debug_db.get_run("does-not-exist") is None


def test_tool_result_records_truncation(home, monkeypatch):
    # A tool result larger than the cap is stored in full, but the entry flags how
    # many chars the model did NOT see. Use tight_context so the cap is the flat
    # TIGHT_TOOL_RESULT_CHARS (the non-tight cap scales up with the budget).
    monkeypatch.setattr(loop, "TIGHT_TOOL_RESULT_CHARS", 100)
    monkeypatch.setattr(loop.egress, "stream", _fake_stream(tool=True))
    big = "Z" * 250

    async def ping(query: str = "") -> str:
        return big
    registry = {"ping": {"fn": ping, "schema": {"type": "function", "function": {
        "name": "ping", "parameters": {"type": "object",
                                       "properties": {"query": {"type": "string"}}}}}}}
    events = _run(tools=registry, session_id="chat:trunc", agent_id="a1",
                  mcp_tool_cap=None, compression=False, tight_context=True)
    run_id = next(e for e in events if e["type"] == "done")["runId"]
    # The live event carries the withheld-char count (250 - 100 = 150).
    tr_ev = next(e for e in events if e["type"] == "tool_result")
    assert tr_ev["truncatedChars"] == 150
    # And the persisted entry keeps the full result + the truncation flag.
    entries = debug_db.get_run(run_id)["run"]["entries"]
    result_entry = next(e for e in entries if e["type"] == "tool_result")
    assert result_entry["toolResult"] == big  # full result retained
    assert result_entry["truncatedChars"] == 150


def test_tool_result_no_truncation_when_small(home, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", _fake_stream(tool=True))
    events = _run(tools=_ping_registry(), session_id="chat:ok", agent_id="a1")
    run_id = next(e for e in events if e["type"] == "done")["runId"]
    entries = debug_db.get_run(run_id)["run"]["entries"]
    result_entry = next(e for e in entries if e["type"] == "tool_result")
    assert result_entry["truncatedChars"] == 0


# --------------------------------------------------------------------------- #
# wiki_read gets its own (configurable) cap, larger than the general tool cap
# --------------------------------------------------------------------------- #

def _named_tool_stream(tool_name: str):
    """One scripted turn that calls ``tool_name`` then answers."""
    calls = {"n": 0}

    async def stream(payload, provider=None):
        turn = calls["n"]
        calls["n"] += 1
        if turn == 0:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": tool_name, "arguments": "{}"}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return stream


def _one_tool_registry(name: str, result: str):
    async def fn(**kw):
        return result
    return {name: {"fn": fn, "schema": {"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}}}


def _truncated_for(home_tools, **run_kw):
    events = _run(**run_kw)
    run_id = next(e for e in events if e["type"] == "done")["runId"]
    entry = next(e for e in debug_db.get_run(run_id)["run"]["entries"]
                 if e["type"] == "tool_result")
    return entry["truncatedChars"], entry["toolResult"]


def test_wiki_read_uses_larger_wiki_cap(home, monkeypatch):
    # 7000 chars: past the general 6000 cap but within the 8000 wiki default →
    # a wiki_read reaches the model intact, a general tool of the same size is clipped.
    monkeypatch.setattr(loop.egress, "stream", _named_tool_stream("wiki_read"))
    big = "W" * 7000
    trunc, full = _truncated_for(None, tools=_one_tool_registry("wiki_read", big),
                                 session_id="chat:wiki", agent_id="a1")
    assert full == big            # full page kept in the trace
    assert trunc == 0             # NOT clipped for the model (7000 < 8000 wiki cap)


def test_general_tool_still_capped_at_6000(home, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", _named_tool_stream("other_tool"))
    big = "P" * 7000
    trunc, _ = _truncated_for(None, tools=_one_tool_registry("other_tool", big),
                              session_id="chat:gen", agent_id="a1", compression=False)
    assert trunc == 1000          # clipped by the general 6000 cap (7000 - 6000)


def test_wiki_tool_cap_override(home, monkeypatch):
    # The per-agent wikiReadChars guardrail flows in as wiki_tool_cap.
    monkeypatch.setattr(loop.egress, "stream", _named_tool_stream("wiki_read"))
    big = "W" * 5000
    trunc, _ = _truncated_for(None, tools=_one_tool_registry("wiki_read", big),
                              session_id="chat:wcap", agent_id="a1", wiki_tool_cap=3000,
                              compression=False)
    assert trunc == 2000          # capped at the override (5000 - 3000)
