"""Recovery from content-empty model turns (the chat channel "(no response)" incident,
2026-08-25). A reasoning model (DeepSeek at high effort) can end a long run — or
even start one — with a turn whose output is all ``reasoning_content`` and no
``content``. The loop must retry such turns (bounded, at low effort), trace them,
and the runner must fall back to the last interim narration instead of returning
an empty reply. See agent/loop.py:run and agent/runner.py:execute_run_streamed."""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop, runner


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _scripted_stream(turns, payloads=None):
    """Each element of ``turns`` is the list of chunks one model call streams.
    ``payloads`` (optional list) collects every request payload for inspection."""
    calls = {"n": 0}

    async def stream(payload, provider=None):
        if payloads is not None:
            payloads.append(payload)
        i = min(calls["n"], len(turns) - 1)
        calls["n"] += 1
        for ch in turns[i]:
            yield ch
    return stream


_EMPTY_TURN = [_chunk(delta={"reasoning_content": "silent pondering"}),
               _chunk(finish="stop", usage={"completion_tokens": 50})]
_SILENT_TURN = [_chunk(finish="stop")]  # nothing at all, not even reasoning


def _drive(stream, monkeypatch, session_id="chat:empty-1", **kw):
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = []

    async def collect():
        async for ev in loop.run([{"role": "user", "content": "q"}], model="m",
                                 tools={}, session_id=session_id, **kw):
            events.append(ev)
    asyncio.run(collect())
    return events


def _final_text(events):
    return "".join(e.get("delta", "") for e in events if e["type"] == "text")


def test_two_empty_turns_then_answer_is_recovered(home, monkeypatch):
    answer = [_chunk(delta={"content": "Here is the answer."}), _chunk(finish="stop")]
    events = _drive(_scripted_stream([_EMPTY_TURN, _EMPTY_TURN, answer]), monkeypatch)
    assert _final_text(events) == "Here is the answer."
    entries = debug_db.get_session("chat:empty-1")["runs"][0]["entries"]
    nudges = [e for e in entries if e["type"] == "system" and "empty-reply nudge" in e["text"]]
    assert len(nudges) == 2


def test_empty_first_turn_without_tools_is_retried(home, monkeypatch):
    # Incident signature B: the FIRST model call streams nothing (0 tool calls).
    # The old guard required tool_count > 0 and ended the run silently.
    answer = [_chunk(delta={"content": "Recovered."}), _chunk(finish="stop")]
    events = _drive(_scripted_stream([_SILENT_TURN, answer]), monkeypatch)
    assert _final_text(events) == "Recovered."


def test_nudged_retries_are_tiered_low_effort_last(home, monkeypatch):
    # Retry 1 keeps the configured effort (quality first); only retry 2 drops
    # to low as a last resort. Remote reasoning turns also get the raised
    # output budget instead of the local-tuned default.
    payloads = []
    answer = [_chunk(delta={"content": "ok"}), _chunk(finish="stop")]
    stream = _scripted_stream([_EMPTY_TURN, _EMPTY_TURN, answer], payloads)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools=["read_file"], session_id="chat:empty-2",
                                reasoning_effort="high", provider={"provider": "x"}):
            pass
    asyncio.run(collect())
    assert [p["reasoning_effort"] for p in payloads] == ["high", "high", "low"]
    assert payloads[0]["max_tokens"] == loop.REASONING_MIN_OUTPUT_TOKENS
    # Payload shape must not change mid-conversation (remote translators reject
    # a tool-result history without a tools param) — schemas stay.
    assert all(("tools" in p) == ("tools" in payloads[0]) for p in payloads)


def test_per_agent_output_budget_overrides_auto(home, monkeypatch):
    # A per-agent maxOutputTokens beats both the env default and the remote
    # reasoning floor.
    payloads = []
    answer = [_chunk(delta={"content": "ok"}), _chunk(finish="stop")]
    monkeypatch.setattr(loop.egress, "stream", _scripted_stream([answer], payloads))

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools={}, session_id="chat:empty-11",
                                reasoning_effort="high", provider={"provider": "x"},
                                max_output_tokens=9000):
            pass
    asyncio.run(collect())
    assert payloads[0]["max_tokens"] == 9000


def test_budget_burned_retry_doubles_budget_at_same_effort(home, monkeypatch):
    # A turn that burned its whole budget reasoning (finish "length", no text)
    # retries with DOUBLE the budget at the configured effort — more room, not
    # degraded quality.
    payloads = []
    answer = [_chunk(delta={"content": "ok"}), _chunk(finish="stop")]
    stream = _scripted_stream([_BURNED_TURN, answer], payloads)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools={}, session_id="chat:empty-10",
                                reasoning_effort="high", provider={"provider": "x"}):
            pass
    asyncio.run(collect())
    assert [p["reasoning_effort"] for p in payloads] == ["high", "high"]
    assert payloads[1]["max_tokens"] == 2 * payloads[0]["max_tokens"]


def test_nudged_retry_never_reenables_disabled_thinking(home, monkeypatch):
    # An agent configured with effort "off" (thinking explicitly disabled) must
    # stay off on the retry — "low" would silently turn thinking back on.
    payloads = []
    answer = [_chunk(delta={"content": "ok"}), _chunk(finish="stop")]
    monkeypatch.setattr(loop.egress, "stream",
                        _scripted_stream([_SILENT_TURN, answer], payloads))

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools={}, session_id="chat:empty-5",
                                reasoning_effort="off", provider={"provider": "x"}):
            pass
    asyncio.run(collect())
    assert [p["reasoning_effort"] for p in payloads] == ["off", "off"]


def test_low_effort_is_one_shot_when_retry_recovers_via_tools(home, monkeypatch):
    # If the nudged retry recovers by CALLING A TOOL, the rest of the run must
    # get its configured effort back — low effort is for the retry only, not a
    # permanent downgrade of the whole investigation.
    async def probe(**kw):
        return "probe result"
    registry = {"probe": {"fn": probe, "schema": {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object", "properties": {}}}}}}
    tool_turn = [_chunk(delta={"tool_calls": [{
        "index": 0, "id": "c1", "type": "function",
        "function": {"name": "probe", "arguments": "{}"}}]}),
        _chunk(finish="tool_calls")]
    answer = [_chunk(delta={"content": "done"}), _chunk(finish="stop")]
    payloads = []
    monkeypatch.setattr(loop.egress, "stream",
                        _scripted_stream([_EMPTY_TURN, _EMPTY_TURN, tool_turn, answer],
                                         payloads))

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools=registry, session_id="chat:empty-4",
                                reasoning_effort="high", provider={"provider": "x"}):
            pass
    asyncio.run(collect())
    assert [p["reasoning_effort"] for p in payloads] == ["high", "high", "low", "high"]


def test_thinking_only_final_turn_is_traced(home, monkeypatch):
    # All retries stay reasoning-only: the run ends empty, but the trace must
    # still show the model's thinking — an unrecorded final turn is undebuggable.
    _drive(_scripted_stream([_EMPTY_TURN]), monkeypatch, session_id="chat:empty-3")
    entries = debug_db.get_session("chat:empty-3")["runs"][0]["entries"]
    thinking = [e for e in entries if e["type"] == "assistant" and e.get("thinking")]
    assert thinking and thinking[-1]["thinking"] == "silent pondering"
    assert thinking[-1]["text"] == ""


_BURNED_TURN = [_chunk(delta={"reasoning_content": "endless pondering"}),
                _chunk(finish="length", usage={"completion_tokens": 3072})]


def test_reasoning_burned_length_turn_is_nudged_to_recovery(home, monkeypatch):
    # A turn that spent its whole output budget reasoning (finish "length",
    # no text) must go through the nudge — the low-effort retry usually
    # produces the real answer — not return a bare cut-off note.
    answer = [_chunk(delta={"content": "The real answer."}), _chunk(finish="stop")]
    events = _drive(_scripted_stream([_BURNED_TURN, answer]), monkeypatch,
                    session_id="chat:empty-8")
    assert _final_text(events) == "The real answer."


def test_reasoning_burned_after_nudges_yields_visible_note(home, monkeypatch):
    # If every retry also burns out, the user still sees an honest note
    # instead of an empty reply.
    events = _drive(_scripted_stream([_BURNED_TURN]), monkeypatch,
                    session_id="chat:empty-9")
    assert "entire output budget reasoning" in _final_text(events)


def test_error_chunk_in_stream_surfaces_as_run_error(home, monkeypatch):
    # A gateway can 200 the SSE handshake and then stream {"error": {...}}.
    # Swallowing it looks like a silent empty turn; it must surface as a run
    # error carrying the gateway's message.
    async def stream(payload, provider=None):
        yield {"error": {"message": "upstream timeout", "code": 504}}
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = []

    async def collect():
        async for ev in loop.run([{"role": "user", "content": "q"}], model="m",
                                 tools={}, session_id="chat:empty-7"):
            events.append(ev)
    asyncio.run(collect())
    errs = [e for e in events if e["type"] == "error"]
    assert errs and "upstream timeout" in errs[0]["message"]


# --- runner: narration fallback -------------------------------------------------

def _fake_stream_run(events):
    async def stream_run(agent, messages, session_id="", extra_system=None):
        for ev in events:
            yield ev
    return stream_run


def _collect_run(events, monkeypatch):
    monkeypatch.setattr(runner, "stream_run", _fake_stream_run(events))
    return asyncio.run(runner.execute_run_streamed({}, "q", "s1"))


def test_runner_falls_back_to_last_narration_when_final_turn_is_empty(home, monkeypatch):
    text, err, _ = _collect_run([
        {"type": "text", "delta": "Found the exact error source in batch rules."},
        {"type": "tool_call", "name": "repo_grep"},
        {"type": "tool_result", "name": "repo_grep"},
        {"type": "done", "runId": "r1"},          # final turn produced no text
    ], monkeypatch)
    assert err is None
    assert "Found the exact error source" in text
    assert "stopped before writing a final answer" in text


def test_runner_normal_final_answer_has_no_fallback_note(home, monkeypatch):
    text, err, _ = _collect_run([
        {"type": "text", "delta": "Let me check."},
        {"type": "tool_call", "name": "repo_grep"},
        {"type": "tool_result", "name": "repo_grep"},
        {"type": "text", "delta": "The real answer."},
        {"type": "done", "runId": "r1"},
    ], monkeypatch)
    assert text == "The real answer."              # narration still doesn't leak
    assert err is None


def test_runner_stash_skips_announcement_only_narration(home, monkeypatch):
    # "Let me check…" delivered as the fallback would read as the answer.
    # Announcements never overwrite the stash, so an earlier substantive
    # finding survives a later announcement — and a run whose only narration
    # was announcements falls back to nothing (honest "(no response)").
    text, err, _ = _collect_run([
        {"type": "text", "delta": "Found the root cause in the batch rule."},
        {"type": "tool_call", "name": "repo_grep"},
        {"type": "text", "delta": "Let me check the shard config."},
        {"type": "tool_call", "name": "repo_read_file"},
        {"type": "done", "runId": "r1"},
    ], monkeypatch)
    assert "Found the root cause" in text and "Let me check" not in text

    text, err, _ = _collect_run([
        {"type": "text", "delta": "Let me check the shard config."},
        {"type": "tool_call", "name": "repo_read_file"},
        {"type": "done", "runId": "r1"},
    ], monkeypatch)
    assert text == ""


def test_step_limit_conclusion_keeps_configured_effort_and_budget(home, monkeypatch):
    # The step-cap conclusion turn is the FIRST (and only) attempt at the
    # final synthesis of a long run — it keeps the configured effort and gets
    # the reasoning-aware output budget; burnout there is handled by the
    # budget, not by preemptively degrading quality.
    async def probe(**kw):
        return "probe result"
    registry = {"probe": {"fn": probe, "schema": {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object", "properties": {}}}}}}
    tool_turn = [_chunk(delta={"tool_calls": [{
        "index": 0, "id": "c1", "type": "function",
        "function": {"name": "probe", "arguments": "{}"}}]}),
        _chunk(finish="tool_calls")]
    answer = [_chunk(delta={"content": "wrap"}), _chunk(finish="stop")]
    payloads = []
    monkeypatch.setattr(loop.egress, "stream",
                        _scripted_stream([tool_turn, answer], payloads))

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "q"}], model="m",
                                tools=registry, session_id="chat:empty-6",
                                max_iterations=1, wrapup_warn_at=0,
                                reasoning_effort="high", provider={"provider": "x"}):
            pass
    asyncio.run(collect())
    assert payloads[0]["reasoning_effort"] == "high"    # the real work
    assert payloads[-1]["reasoning_effort"] == "high"   # conclusion: quality kept
    assert payloads[-1]["max_tokens"] == loop.REASONING_MIN_OUTPUT_TOKENS


def test_runner_error_is_not_masked_by_narration(home, monkeypatch):
    text, err, _ = _collect_run([
        {"type": "text", "delta": "Partial progress."},
        {"type": "tool_call", "name": "repo_grep"},
        {"type": "error", "message": "boom"},
        {"type": "done", "runId": "r1"},
    ], monkeypatch)
    assert err == "boom"
    assert text == ""                              # error path stays honest
