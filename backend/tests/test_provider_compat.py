"""Provider-compat experiment slice — the OpenRouter-dialect reasoning layer for
the custom OpenAI-compatible provider: record fields, egress body shaping, and
the loop's streamed-reasoning capture. All hermetic (no network)."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from gateway import egress, providers


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


# --- set_record round-trip -------------------------------------------------- #

def test_thinking_format_and_replay_round_trip(home):
    # A legacy scalar string lands on the "*" default row (map form).
    providers.set_record("openai-compatible", thinkingFormat="openrouter",
                         replayReasoning=True)
    rec = providers.get_record("openai-compatible")
    assert rec["thinkingFormat"] == {"*": "openrouter"}
    assert rec["replayReasoning"] == {"*": True}
    cat = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert cat["thinkingFormat"] == {"*": "openrouter"}
    assert cat["replayReasoning"] == {"*": True}


def test_replay_reasoning_per_model_map_merge_and_clear(home):
    # Per-model rows + a "*" default, merged across writes.
    providers.set_record("openai-compatible",
                         replayReasoning={"glm-5.2": True, "*": True})
    assert providers.get_record("openai-compatible")["replayReasoning"] == {
        "glm-5.2": True, "*": True}
    # A second write merges without dropping existing rows.
    providers.set_record("openai-compatible", replayReasoning={"kimi-k3": True})
    assert providers.get_record("openai-compatible")["replayReasoning"] == {
        "glm-5.2": True, "*": True, "kimi-k3": True}
    # A False row value stores an explicit off (beats the "*" row).
    providers.set_record("openai-compatible", replayReasoning={"glm-5.2": False})
    assert providers.get_record("openai-compatible")["replayReasoning"]["glm-5.2"] is False
    assert providers.replay_reasoning_for("openai-compatible", "glm-5.2") is False
    # null removes the row entirely (back to inherit).
    providers.set_record("openai-compatible", replayReasoning={"glm-5.2": None})
    assert "glm-5.2" not in providers.get_record("openai-compatible")["replayReasoning"]
    assert providers.replay_reasoning_for("openai-compatible", "glm-5.2") is True  # via "*"


def test_replay_reasoning_legacy_scalar_migrates_on_next_write(home):
    # Simulate a config file that still carries the pre-map scalar shape.
    from storage import settings
    settings.set("providers", {"openai-compatible": {"replayReasoning": True}})
    # Resolution reads the legacy scalar as the "*" row.
    assert providers.replay_reasoning_for("openai-compatible", "deepseek") is True
    # Any set_record write normalizes the stored value forward to map form.
    providers.set_record("openai-compatible", replayReasoning={"glm-5.2": True})
    assert providers.get_record("openai-compatible")["replayReasoning"] == {
        "*": True, "glm-5.2": True}


def test_replay_reasoning_for_precedence(home):
    providers.set_record("openai-compatible",
                         replayReasoning={"glm-5.2": True})
    # exact row wins
    assert providers.replay_reasoning_for("openai-compatible", "glm-5.2") is True
    # no "*" row, no exact row → off
    assert providers.replay_reasoning_for("openai-compatible", "deepseek") is False
    # non-custom provider never resolves replay
    assert providers.replay_reasoning_for("deepseek", "deepseek-chat") is False


def test_replay_reasoning_explicit_off_beats_default(home):
    # All on via "*", one model explicitly off; and the inverse.
    providers.set_record("openai-compatible",
                         replayReasoning={"*": True, "gpt-5.5": False})
    assert providers.replay_reasoning_for("openai-compatible", "gpt-5.5") is False
    assert providers.replay_reasoning_for("openai-compatible", "kimi-k3") is True
    providers.set_record("openai-compatible",
                         replayReasoning={"*": False, "kimi-k3": True})
    assert providers.replay_reasoning_for("openai-compatible", "kimi-k3") is True
    assert providers.replay_reasoning_for("openai-compatible", "gpt-5.5") is False


def test_caching_and_vision_explicit_off_beats_default(home):
    providers.set_record("openai-compatible",
                         caching={"*": True, "gpt-5.5": False},
                         vision={"*": True, "gpt-5.5": False})
    assert providers.caching_enabled("openai-compatible", "gpt-5.5") is False
    assert providers.caching_enabled("openai-compatible", "kimi-k3") is True
    assert providers.supports_vision("openai-compatible", "gpt-5.5") is False
    assert providers.supports_vision("openai-compatible", "kimi-k3") is True
    # null removes the row (inherit again).
    providers.set_record("openai-compatible",
                         caching={"gpt-5.5": None}, vision={"gpt-5.5": None})
    assert providers.caching_enabled("openai-compatible", "gpt-5.5") is True
    assert providers.supports_vision("openai-compatible", "gpt-5.5") is True


def test_replay_reasoning_rejects_invalid_value(home):
    with pytest.raises(ValueError):
        providers.set_record("openai-compatible", replayReasoning="yes")


def test_thinking_format_per_model_map_merge_and_clear(home):
    # Per-model rows + a "*" default, merged across writes.
    providers.set_record("openai-compatible",
                         thinkingFormat={"glm-5.2": "zai", "*": "openrouter"})
    assert providers.get_record("openai-compatible")["thinkingFormat"] == {
        "glm-5.2": "zai", "*": "openrouter"}
    # A second write merges (adds a row) without dropping existing rows.
    providers.set_record("openai-compatible", thinkingFormat={"kimi-k3": "openrouter"})
    assert providers.get_record("openai-compatible")["thinkingFormat"] == {
        "glm-5.2": "zai", "*": "openrouter", "kimi-k3": "openrouter"}
    # A blank row value removes just that row.
    providers.set_record("openai-compatible", thinkingFormat={"glm-5.2": ""})
    assert "glm-5.2" not in providers.get_record("openai-compatible")["thinkingFormat"]
    # Clearing to "" drops the whole key.
    providers.set_record("openai-compatible", thinkingFormat="")
    assert "thinkingFormat" not in providers.get_record("openai-compatible")


def test_thinking_format_legacy_scalar_migrates_on_next_write(home):
    # Simulate a config file that still carries the pre-map scalar shape.
    from storage import settings
    settings.set("providers", {"openai-compatible": {"thinkingFormat": "openrouter"}})
    # Any set_record write normalizes the stored value forward to map form.
    providers.set_record("openai-compatible", thinkingFormat={"glm-5.2": "zai"})
    assert providers.get_record("openai-compatible")["thinkingFormat"] == {
        "*": "openrouter", "glm-5.2": "zai"}


def test_thinking_format_for_precedence(home):
    providers.set_record("openai-compatible",
                         thinkingFormat={"glm-5.2": "zai", "*": "openrouter"})
    # exact row wins
    assert providers.thinking_format_for("openai-compatible", "glm-5.2") == "zai"
    # falls back to the "*" default row
    assert providers.thinking_format_for("openai-compatible", "deepseek") == "openrouter"
    # non-custom provider never resolves a format
    assert providers.thinking_format_for("deepseek", "deepseek-chat") is None


def test_thinking_format_default_and_explicit_openai_for_custom_gateway(home):
    # The UI's default Thinking choice is OpenAI even before a row is stored;
    # no row remains unset in storage for backwards compatibility.
    assert providers.thinking_format_for("openai-compatible", "any-model") is None
    providers.set_record("openai-compatible", thinkingFormat={"*": "openai"})
    assert providers.thinking_format_for("openai-compatible", "any-model") == "openai"
    providers.set_record("openai-compatible", thinkingFormat={"glm-5.2": "zai"})
    assert providers.thinking_format_for("openai-compatible", "glm-5.2") == "zai"
    assert providers.thinking_format_for("openai-compatible", "other") == "openai"


def test_thinking_format_map_rejects_invalid_value(home):
    with pytest.raises(ValueError):
        providers.set_record("openai-compatible", thinkingFormat={"glm-5.2": "bogus"})


def test_resolve_target_per_model_replay(home):
    # Replay opted in for one model only — resolve_target stamps per-model.
    providers.set_record("openai-compatible", baseUrl="http://127.0.0.1:15721/v1",
                         authMode="none", apiType="responses",
                         replayReasoning={"glm-5.2": True})
    _, t_glm = asyncio.run(providers.resolve_target(
        {"modelSource": "byok", "provider": "openai-compatible", "model": "glm-5.2"}))
    assert t_glm["replay_reasoning"] is True
    assert t_glm["stateless_reasoning_replay"] is True
    _, t_ds = asyncio.run(providers.resolve_target(
        {"modelSource": "byok", "provider": "openai-compatible", "model": "deepseek"}))
    assert t_ds["replay_reasoning"] is False
    assert t_ds["stateless_reasoning_replay"] is False


def test_thinking_format_clears_and_replay_stores_truthy_only(home):
    providers.set_record("openai-compatible", thinkingFormat="openrouter",
                         replayReasoning=True)
    # Scalar False writes an explicit-off "*" row (tri-state semantics).
    providers.set_record("openai-compatible", replayReasoning=False)
    assert providers.get_record("openai-compatible")["replayReasoning"] == {"*": False}
    assert providers.replay_reasoning_for("openai-compatible", "any") is False
    # null on the "*" row clears the key entirely.
    providers.set_record("openai-compatible", replayReasoning={"*": None})
    assert "replayReasoning" not in providers.get_record("openai-compatible")
    # Empty string clears the format back to the plain default.
    providers.set_record("openai-compatible", thinkingFormat="")
    assert "thinkingFormat" not in providers.get_record("openai-compatible")


def test_thinking_format_rejects_unknown_value(home):
    with pytest.raises(ValueError):
        providers.set_record("openai-compatible", thinkingFormat="deepseek")


def test_non_custom_provider_rejects_compat_fields(home):
    with pytest.raises(ValueError):
        providers.set_record("deepseek", thinkingFormat="openrouter")
    with pytest.raises(ValueError):
        providers.set_record("deepseek", replayReasoning=True)


# --- resolve_target stamps the fields on the target ------------------------- #

def test_resolve_target_stamps_per_model_resolved_scalar(home):
    # One gateway, two dialects — resolve_target stamps the scalar for THIS model.
    providers.set_record("openai-compatible", baseUrl="http://127.0.0.1:15721/v1",
                         authMode="none", replayReasoning=True,
                         thinkingFormat={"glm-5.2": "zai", "*": "openrouter"})
    _, t_glm = asyncio.run(providers.resolve_target(
        {"modelSource": "byok", "provider": "openai-compatible", "model": "glm-5.2"}))
    assert t_glm["thinking_format"] == "zai"
    assert t_glm["replay_reasoning"] is True
    _, t_ds = asyncio.run(providers.resolve_target(
        {"modelSource": "byok", "provider": "openai-compatible", "model": "deepseek"}))
    assert t_ds["thinking_format"] == "openrouter"  # via the "*" row


# --- _openai_body reasoning shaping ----------------------------------------- #

def test_openai_body_openrouter_effort():
    target = {"provider": "openai-compatible", "thinking_format": "openrouter"}
    body = egress._openai_body({"model": "deepseek", "reasoning_effort": "high"}, target)
    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body


def test_openai_body_openrouter_unset_sends_nothing():
    # "" = model default: leave the model's native thinking untouched.
    target = {"provider": "openai-compatible", "thinking_format": "openrouter"}
    body = egress._openai_body({"model": "deepseek"}, target)
    assert "reasoning" not in body
    assert "reasoning_effort" not in body


def test_openai_body_openrouter_off_is_explicit():
    # Pi/OpenRouter express explicit "off" as the native no-reasoning effort.
    target = {"provider": "openai-compatible", "thinking_format": "openrouter"}
    body = egress._openai_body({"model": "deepseek", "reasoning_effort": "off"}, target)
    assert body["reasoning"] == {"effort": "none"}
    assert "reasoning_effort" not in body


def test_openai_body_zai_enabled_with_effort():
    # zai: effort truthy → thinking enabled, and NEVER a reasoning/effort param.
    target = {"provider": "openai-compatible", "thinking_format": "zai"}
    body = egress._openai_body({"model": "glm-5.2", "reasoning_effort": "high"}, target)
    assert body["thinking"] == {"type": "enabled"}
    assert "reasoning" not in body
    assert "reasoning_effort" not in body


def test_openai_body_zai_unset_sends_nothing():
    target = {"provider": "openai-compatible", "thinking_format": "zai"}
    body = egress._openai_body({"model": "glm-5.2"}, target)
    assert "thinking" not in body
    assert "reasoning" not in body
    assert "reasoning_effort" not in body


def test_openai_body_zai_off_is_explicit():
    target = {"provider": "openai-compatible", "thinking_format": "zai"}
    body = egress._openai_body({"model": "glm-5.2", "reasoning_effort": "off"}, target)
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning" not in body
    assert "reasoning_effort" not in body


def test_openai_body_plain_openai_never_receives_off():
    # No managed dialect: "off" must not leak as a literal reasoning_effort value.
    body = egress._openai_body({"model": "gpt-x", "reasoning_effort": "off"},
                               {"provider": "openai"})
    assert "reasoning_effort" not in body


def test_openai_body_custom_openai_format_requires_forward_toggle():
    payload = {"model": "gpt-x", "reasoning_effort": "high", "temperature": 0.7}
    body = egress._openai_body(
        payload, {"provider": "openai-compatible", "thinking_format": "openai",
                  "reasoning_effort": True})
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body

    body = egress._openai_body(
        payload, {"provider": "openai-compatible", "thinking_format": "openai",
                  "reasoning_effort": False})
    assert "reasoning_effort" not in body
    assert body["temperature"] == 0.7


def test_openai_body_custom_openai_format_never_sends_off():
    body = egress._openai_body(
        {"model": "gpt-x", "reasoning_effort": "off"},
        {"provider": "openai-compatible", "thinking_format": "openai",
         "reasoning_effort": True})
    assert "reasoning_effort" not in body


def test_openai_body_unset_format_is_byte_identical():
    # No thinking_format → today's behavior: no `reasoning` key, and reasoning_effort
    # dropped for a non-openai compatible provider.
    target = {"provider": "openai-compatible"}
    payload = {"model": "deepseek", "reasoning_effort": "high", "temperature": 0.7}
    body = egress._openai_body(payload, target)
    assert "reasoning" not in body
    assert "reasoning_effort" not in body
    assert body == {"model": "deepseek", "temperature": 0.7}


# --- streamed reasoning capture --------------------------------------------- #

def _drain(agen):
    async def run():
        out = []
        async for item in agen:
            out.append(item)
        return out
    return asyncio.run(run())


def test_stream_turn_accumulates_reasoning(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning": "Let me "}}]},
        {"choices": [{"delta": {"reasoning": "think."}}]},
        {"choices": [{"delta": {"content": "Answer"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text",
                                                        "text": "Let me think."}]},
                      "finish_reason": "stop"}]},
    ]

    async def fake_stream(payload, provider):
        for c in chunks:
            yield c

    monkeypatch.setattr(egress, "stream", fake_stream)
    events = _drain(loop._stream_turn({"model": "x"}, {"provider": "openai-compatible"}))
    end = next(data for kind, data in events if kind == "end")
    assert end["thinking"] == "Let me think."
    assert end["text"] == "Answer"
    assert end["reasoning_details"] == [{"type": "reasoning.text", "text": "Let me think."}]


def test_stream_turn_no_reasoning_leaves_thinking_none(monkeypatch):
    chunks = [{"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}]

    async def fake_stream(payload, provider):
        for c in chunks:
            yield c

    monkeypatch.setattr(egress, "stream", fake_stream)
    events = _drain(loop._stream_turn({"model": "x"}, None))
    end = next(data for kind, data in events if kind == "end")
    assert end["thinking"] is None
    assert end["reasoning_details"] is None


# --- Debug-trace gap + run-level accounting (loop.run integration) ---------- #

def _echo_registry():
    async def echo(x=0):
        return f"echo-{x}"
    return {"echo": {"fn": echo, "schema": {"type": "function", "function": {
        "name": "echo", "parameters": {"type": "object",
                                       "properties": {"x": {"type": "integer"}}}}}}}


def _capture_run(monkeypatch):
    """Capture the single (run, entries) pair loop.run persists via debug_db."""
    captured = {}

    def fake_insert(run, entries):
        captured["run"] = run
        captured["entries"] = entries

    monkeypatch.setattr(loop.debug_db, "insert_run", fake_insert)
    return captured


def _tool_then_answer_stream(*, turn0_delta, turn0_usage, turn1_usage):
    """Turn 0 emits one `echo` tool call with the given delta (reasoning and/or
    content), turn 1 concludes with plain text."""
    state = {"turn": 0}

    async def stream(payload, provider=None):
        i = state["turn"]
        state["turn"] += 1
        if i == 0:
            delta = dict(turn0_delta)
            delta["tool_calls"] = [{"index": 0, "id": "c0", "type": "function",
                                    "function": {"name": "echo",
                                                 "arguments": json.dumps({"x": 1})}}]
            yield {"choices": [{"delta": delta, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                   "usage": turn0_usage}
        else:
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
                   "usage": turn1_usage}
    return stream


def _run_capture(monkeypatch, stream):
    captured = _capture_run(monkeypatch)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def go():
        async for _ in loop.run([{"role": "user", "content": "go"}],
                                model="m", tools=_echo_registry()):
            pass
    asyncio.run(go())
    return captured


def test_textless_toolcall_turn_with_thinking_records_entry(home, monkeypatch):
    stream = _tool_then_answer_stream(
        turn0_delta={"reasoning": "why I call"},
        turn0_usage={"prompt_tokens": 100, "completion_tokens": 5},
        turn1_usage={"prompt_tokens": 150, "completion_tokens": 2})
    captured = _run_capture(monkeypatch, stream)
    entries = captured["entries"]
    # A text-less tool-call turn still produced an assistant entry carrying just
    # the thinking, ordered before its tool_call entry.
    a_idx = next(i for i, e in enumerate(entries)
                 if e["type"] == "assistant" and e.get("thinking") == "why I call")
    assert entries[a_idx]["text"] == ""
    tc_idx = next(i for i, e in enumerate(entries) if e["type"] == "tool_call")
    assert a_idx < tc_idx


def test_textless_toolcall_turn_without_thinking_adds_no_entry(home, monkeypatch):
    stream = _tool_then_answer_stream(
        turn0_delta={},  # no reasoning, no content
        turn0_usage={"prompt_tokens": 100, "completion_tokens": 5,
                     "cacheRead": 80, "cacheWrite": 3},
        turn1_usage={"prompt_tokens": 150, "completion_tokens": 2,
                     "cacheRead": 120})
    captured = _run_capture(monkeypatch, stream)
    # No empty thinking-less assistant entry was inserted for the tool-call turn.
    assert not any(e["type"] == "assistant" and not e.get("text") and not e.get("thinking")
                   for e in captured["entries"])
    # Its usage must still contribute to the run totals even though there is no
    # assistant entry from which _record could reconstruct it.
    assert captured["run"]["cache_read"] == 200
    assert captured["run"]["cache_write"] == 3
    assert captured["run"]["prompt_tokens_sum"] == 250
    assert captured["run"]["total_tokens"] == 257


def test_prompt_tokens_sum_accumulates_across_turns(home, monkeypatch):
    stream = _tool_then_answer_stream(
        turn0_delta={"reasoning": "r"},
        turn0_usage={"prompt_tokens": 100, "completion_tokens": 5},
        turn1_usage={"prompt_tokens": 150, "completion_tokens": 2})
    captured = _run_capture(monkeypatch, stream)
    # input_tokens keeps last-turn semantics; the sum spans both turns.
    assert captured["run"]["input_tokens"] == 150
    assert captured["run"]["prompt_tokens_sum"] == 250


def test_context_window_peak_and_series_recorded(home, monkeypatch):
    """The Debug context meter: peak = the biggest single call's prompt (not the
    sum), the per-call series in order, and the reported window."""
    stream = _tool_then_answer_stream(
        turn0_delta={"reasoning": "r"},
        turn0_usage={"prompt_tokens": 900, "completion_tokens": 5},
        turn1_usage={"prompt_tokens": 400, "completion_tokens": 2})
    captured = _capture_run(monkeypatch)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def go():
        async for _ in loop.run([{"role": "user", "content": "go"}], model="m",
                                tools=_echo_registry(), report_window=2000):
            pass
    asyncio.run(go())
    run = captured["run"]
    assert run["context_series"] == [900, 400]
    assert run["peak_context_tokens"] == 900
    assert run["context_window"] == 2000
