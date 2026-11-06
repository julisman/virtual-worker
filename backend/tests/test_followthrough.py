"""Follow-through nudge + tool-evidence history (anti-hallucination pair).

Two real incidents (2026-07-23):
- Agung2nd replied "Sedang diproses... saya panggil dulu MCP Google Workspace…"
  with zero tool calls — the run ended on a promise nothing would fulfil.
- Ahmad narrated tool use it never performed, imitating its own chat-channel history
  where stitched between-tool narration read as "results appear from text".
"""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}]}


def _scripted_stream(replies):
    """Each call to the model pops the next scripted text reply."""
    calls = {"n": 0}

    async def stream(payload, provider=None):
        calls["n"] += 1
        text = replies.pop(0)
        yield _chunk(delta={"content": text})
        yield _chunk(finish="stop")
    return stream, calls


ECHO_TOOL = {"echo": {"schema": {"type": "function", "function": {
    "name": "echo", "parameters": {"type": "object", "properties": {}}}},
    "fn": None}}


def _collect(stream, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = []

    async def run():
        async for ev in loop.run([{"role": "user", "content": "sudah ?"}],
                                 model="m", tools=dict(ECHO_TOOL)):
            events.append(ev)
    asyncio.run(run())
    return events


def test_announced_action_with_no_tool_call_is_nudged(home, monkeypatch):
    stream, calls = _scripted_stream(
        ["Sedang diproses... saya panggil dulu MCP Google Workspace untuk ambil event hari ini.",
         "Tidak bisa mengakses kalender — tool-nya tidak tersedia di sesi ini."])
    events = _collect(stream, monkeypatch)
    assert calls["n"] == 2                                   # nudged retry happened
    text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    assert "tidak tersedia" in text                          # the real answer arrived


def test_english_promise_is_nudged_once_only(home, monkeypatch):
    stream, calls = _scripted_stream(
        ["Let me check the calendar now.",
         "I'll check it right away.",                        # promises again…
         "never used"])
    _collect(stream, monkeypatch)
    assert calls["n"] == 2                                   # one shot — no nudge loop


def test_plain_answer_is_not_nudged(home, monkeypatch):
    stream, calls = _scripted_stream(["You have 3 events today: standup, 1:1, review."])
    events = _collect(stream, monkeypatch)
    assert calls["n"] == 1                                   # no extra model call
    assert events[-1]["type"] == "done"


# ─── tool-evidence history (chat channels) ─────────────────────────────────────────

def test_load_history_annotates_tool_using_replies(home):
    from channels.manager import load_history as _load_history
    from storage import chat_store
    sid = "whatsapp:acct1:628111"
    chat_store.append_message(sid, "user", "approve this ticket", agent_id="a1")
    chat_store.append_message(
        sid, "assistant", "Let me open it.Done — ticket 123 is pending AM approval.",
        agent_id="a1", meta={"runId": "r1", "toolsUsed": ["browser_open", "browser_read", "browser_read"]})
    chat_store.append_message(sid, "assistant", "Anything else?", agent_id="a1")

    hist = _load_history(sid)
    tool_turn = hist[1]["content"]
    assert tool_turn.startswith("[this reply was produced by actually CALLING tools:")
    assert "browser_read ×2" in tool_turn and "browser_open" in tool_turn
    assert "may be reused without re-calling" in tool_turn
    assert "must come from a fresh tool call" in tool_turn
    assert hist[2]["content"] == "Anything else?"            # plain turns untouched


def test_strip_meta_notes_drops_imitated_provenance_notes():
    from channels.manager import _strip_meta_notes
    echoed = ("[this reply was produced by actually CALLING tools: browser_evaluate ×2 — "
              "every detail came from those tool results. Facts already stated here may be "
              "reused without re-calling tools, but NEVER invent new tool results — "
              "anything not stated here must come from a fresh tool call.]\n"
              "[Nothing has happened yet this turn — I have not called any tools. "
              "Everything below comes from tool calls I make now.]\n"
              "Ticket 123 is pending AM approval.")
    assert _strip_meta_notes(echoed) == "Ticket 123 is pending AM approval."
    # Legitimate bracket openings survive; plain replies pass through.
    assert _strip_meta_notes("[link] see the dashboard") == "[link] see the dashboard"
    assert _strip_meta_notes("All done.") == "All done."
    assert _strip_meta_notes("") == ""


def test_debug_db_tools_used_returns_call_order(home):
    debug_db.insert_run(
        {"run_id": "r9", "session_id": "s", "start_time": "t", "end_time": "t",
         "method": "POST", "path": "/api/chat", "endpoint": "/api/chat",
         "provider": "local", "model": "m", "status_code": 200, "is_error": False,
         "stream": True, "tool_calls": 2},
        [{"type": "tool_call", "tool_name": "browser_open", "timestamp": "t"},
         {"type": "tool_result", "tool_name": "browser_open", "tool_result": "ok", "timestamp": "t"},
         {"type": "tool_call", "tool_name": "browser_read", "timestamp": "t"},
         {"type": "assistant", "text": "done", "timestamp": "t"}])
    assert debug_db.tools_used("r9") == ["browser_open", "browser_read"]
