"""Reasoning guardrail (``guardrails.reasoning: "off"``) — hide <think> output.

Thinking-capable local models (Qwen3-style) emit their reasoning inline in
``content`` unless the server splits it out; with the guardrail off, the loop
must keep it away from the user (streamed deltas AND the final text) and ask
the chat template to skip thinking entirely.
"""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from agent.loop import _ThinkFilter


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


# ─── the incremental filter ──────────────────────────────────────────────────

def _run_filter(chunks):
    f = _ThinkFilter()
    out = "".join(f.feed(c) for c in chunks)
    return out + f.flush()


def test_filter_drops_think_span_and_keeps_answer():
    assert _run_filter(["<think>plan the steps</think>\n\nThe answer is 42."]) == "The answer is 42."


def test_filter_handles_tags_split_across_deltas():
    assert _run_filter(["<thi", "nk>secret ", "reasoning</th", "ink>\nAnswer."]) == "Answer."


def test_filter_drops_unclosed_think_block():
    assert _run_filter(["<think>never closes ", "still thinking…"]) == ""


def test_filter_passes_plain_text_and_literal_angles():
    assert _run_filter(["2 < 3 and a<b", " stays"]) == "2 < 3 and a<b stays"
    assert _run_filter(["no tags at all"]) == "no tags at all"


def test_filter_keeps_text_around_mid_reply_think():
    assert _run_filter(["Hello <think>hmm</think> world"]) == "Hello world"


# ─── loop integration ────────────────────────────────────────────────────────

def _chunk(delta=None, finish=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}]}


def _scripted_stream(deltas):
    """One model call streaming the given content deltas; records the payload."""
    seen = {"payloads": []}

    async def stream(payload, provider=None):
        seen["payloads"].append(payload)
        for d in deltas:
            yield _chunk(delta={"content": d})
        yield _chunk(finish="stop")
    return stream, seen


def _collect(stream, monkeypatch, **kwargs):
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = []

    async def run():
        async for ev in loop.run([{"role": "user", "content": "hi"}],
                                 model="m", **kwargs):
            events.append(ev)
    asyncio.run(run())
    return events


def test_hide_reasoning_strips_think_from_stream(home, monkeypatch):
    stream, seen = _scripted_stream(["<think>let me ", "reason</think>", "Done — 3 events."])
    events = _collect(stream, monkeypatch, hide_reasoning=True)
    text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    assert text == "Done — 3 events."
    assert "reason" not in text
    # The local upstream is asked not to think at all.
    assert seen["payloads"][0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_reasoning_on_is_unchanged(home, monkeypatch):
    stream, seen = _scripted_stream(["<think>hmm</think>", "Answer."])
    events = _collect(stream, monkeypatch)
    text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    assert "<think>hmm</think>" in text                     # legacy passthrough
    assert "chat_template_kwargs" not in seen["payloads"][0]
