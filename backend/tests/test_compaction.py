"""Provider-aware context budget + opt-in auto-compaction (agent/loop.py).

The 40k char budget is sized for the local model; a remote provider with a big
window should get more, an agent can pin an explicit budget, and ``autoCompact``
folds the older turns into ONE LLM-written recap instead of eliding them to stubs.
See loop._resolve_budget / loop._maybe_compact / loop.run."""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from agent.loop import (
    CONTEXT_CHAR_BUDGET, REMOTE_CONTEXT_CHARS, TIGHT_CONTEXT_BUDGET,
    TIGHT_TOOL_RESULT_CHARS, MAX_TOOL_RESULT_CHARS, KEEP_RECENT_MSGS,
    _resolve_budget, _maybe_compact, _msg_chars, _fit_context,
)

PROVIDER = {"kind": "openai", "baseUrl": "https://api.openai.com"}


# --- budget resolution ------------------------------------------------------ #

def test_budget_tight_context_wins_over_everything():
    # tight_context is the explicit "fast" switch — it beats an explicit budget
    # AND a remote provider target.
    ctx, cap = _resolve_budget(True, 999_999, PROVIDER)
    assert ctx == TIGHT_CONTEXT_BUDGET and cap == TIGHT_TOOL_RESULT_CHARS


def test_budget_explicit_context_chars_wins_over_auto():
    ctx, _ = _resolve_budget(False, 100_000, PROVIDER)   # remote, but explicit set
    assert ctx == 100_000
    # re-clamped defensively even though the store already clamps.
    assert _resolve_budget(False, 500, None)[0] == 8_000
    assert _resolve_budget(False, 9_999_999, None)[0] == 2_000_000


def test_budget_auto_local_vs_remote():
    assert _resolve_budget(False, 0, None)[0] == CONTEXT_CHAR_BUDGET       # local
    assert _resolve_budget(False, 0, PROVIDER)[0] == REMOTE_CONTEXT_CHARS  # remote


def test_tool_cap_scales_with_budget():
    # tool_cap = max(MAX_TOOL_RESULT_CHARS, min(24000, ctx // 10)).
    assert _resolve_budget(False, 0, None)[1] == MAX_TOOL_RESULT_CHARS      # 40k//10=4k → floor 6k
    assert _resolve_budget(False, 0, PROVIDER)[1] == 24_000                 # 240k//10=24k (ceil)
    assert _resolve_budget(False, 100_000, None)[1] == 10_000               # 100k//10
    assert _resolve_budget(False, 8_000, None)[1] == MAX_TOOL_RESULT_CHARS  # 8k//10=800 → floor 6k


# --- compaction helper ------------------------------------------------------ #

def _big(role, txt, n=1000):
    return {"role": role, "content": (txt + " ") * (n // len(txt + " ") + 1)}


def _convo():
    """system + task + 8 bulky middle turns + 6 recent turns (18 msgs, ~14k chars)."""
    mid = [_big("assistant" if i % 2 else "user", f"middle{i}") for i in range(8)]
    recent = [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT_MSGS)]
    return ([{"role": "system", "content": "SYSTEM"},
             {"role": "user", "content": "the original task"}] + mid + recent)


def _stub(monkeypatch, ret="RECAP", raises=None):
    calls = {"n": 0, "slices": []}

    async def fake(model, messages, provider=None):
        calls["n"] += 1
        calls["slices"].append(messages)
        if raises is not None:
            raise raises
        return ret
    monkeypatch.setattr(loop, "summarize_messages", fake)
    return calls


def test_compact_triggers_and_keeps_head_and_tail_verbatim(monkeypatch):
    calls = _stub(monkeypatch, ret="COMPACT-RECAP")
    convo = _convo()
    budget = 8_000
    assert sum(_msg_chars(m) for m in convo) > budget * 80 / 100  # over threshold
    out, entry, last = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=budget, compact_at=80,
        last_compact_len=-1))
    assert calls["n"] == 1
    # system + task kept verbatim, then ONE recap message right after the task.
    assert out[0] == {"role": "system", "content": "SYSTEM"}
    assert out[1] == {"role": "user", "content": "the original task"}
    assert out[2]["role"] == "user"
    assert out[2]["content"].startswith("[Recap of earlier conversation")
    assert "COMPACT-RECAP" in out[2]["content"]
    # the recent tail is preserved verbatim.
    assert out[-KEEP_RECENT_MSGS:] == convo[-KEEP_RECENT_MSGS:]
    assert entry["type"] == "compaction" and entry["success"] is True
    assert last == len(out)


def test_compact_skips_below_threshold(monkeypatch):
    calls = _stub(monkeypatch)
    convo = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out, entry, last = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=100_000, compact_at=80,
        last_compact_len=-1))
    assert out is convo and entry is None and calls["n"] == 0


def test_compact_pair_safety_never_splits_tool_group(monkeypatch):
    # An assistant carrying tool_calls sits at the LAST foldable slot; its tool
    # result is the first message of the kept tail. The boundary must move earlier
    # so the pair stays together in the verbatim tail (never folded apart).
    _stub(monkeypatch, ret="R")
    asst = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}]}
    toolres = {"role": "tool", "tool_call_id": "t1", "content": "Z" * 2000}
    mid = [_big("user", f"m{i}") for i in range(6)]
    tail = [{"role": "user", "content": f"r{i}"} for i in range(KEEP_RECENT_MSGS - 1)]
    convo = ([{"role": "system", "content": "S"},
              {"role": "user", "content": "task"}] + mid + [asst, toolres] + tail)
    out, entry, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=-1))
    # the assistant+tool pair survives verbatim and adjacent in the output.
    ai = next(i for i, m in enumerate(out) if m.get("tool_calls"))
    assert out[ai] == asst and out[ai + 1] == toolres


def test_compact_pair_safety_boundary_mid_group_leaves_no_orphan_tools(monkeypatch):
    # The tail boundary lands BETWEEN two tool results of one assistant (multi
    # tool-call turn). Folding must back up past the whole group — a kept tail
    # starting with orphan "tool" messages is rejected by OpenAI-shape providers.
    _stub(monkeypatch, ret="R")
    asst = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}},
                           {"id": "t2", "type": "function",
                            "function": {"name": "y", "arguments": "{}"}}]}
    tool1 = {"role": "tool", "tool_call_id": "t1", "content": "A" * 1500}
    tool2 = {"role": "tool", "tool_call_id": "t2", "content": "B" * 1500}
    mid = [_big("user", f"m{i}") for i in range(6)]
    tail = [{"role": "user", "content": f"r{i}"} for i in range(KEEP_RECENT_MSGS - 1)]
    convo = ([{"role": "system", "content": "S"},
              {"role": "user", "content": "task"}] + mid + [asst, tool1, tool2] + tail)
    # boundary (n - KEEP_RECENT_MSGS) lands on tool2 — mid-group.
    assert convo[len(convo) - KEEP_RECENT_MSGS] is tool2
    out, _, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=-1))
    # the whole group survives verbatim and adjacent; no tool message anywhere
    # in the output lacks its preceding tool_calls assistant.
    ai = next(i for i, m in enumerate(out) if m.get("tool_calls"))
    assert out[ai] == asst and out[ai + 1] == tool1 and out[ai + 2] == tool2
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert any(out[j].get("tool_calls") for j in range(i)), "orphan tool msg"


def test_compact_once_no_retry_without_growth(monkeypatch):
    # A successful fold shrinks the convo below threshold → a second pass on the
    # returned convo doesn't call the summarizer again.
    calls = _stub(monkeypatch, ret="RECAP")
    convo = _convo()
    out, _, last = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=-1))
    assert calls["n"] == 1
    out2, entry2, _ = asyncio.run(_maybe_compact(
        out, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=last))
    assert calls["n"] == 1 and entry2 is None       # not called again


def test_compact_failure_does_not_retry_every_iteration(monkeypatch):
    # An empty summary falls back; the convo is unchanged and still over budget,
    # but the compact-once guard blocks a retry until the convo grows.
    calls = _stub(monkeypatch, ret="")
    convo = _convo()
    out, entry, last = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=-1))
    assert calls["n"] == 1 and entry["success"] is False and last == len(convo)
    # second pass, same convo (no growth) → guard skips before calling.
    asyncio.run(_maybe_compact(
        out, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=last))
    assert calls["n"] == 1


def test_compact_summarizer_raising_falls_back_and_fit_context_still_fits(monkeypatch):
    _stub(monkeypatch, raises=RuntimeError("boom"))
    convo = _convo()
    out, entry, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=8_000, compact_at=80,
        last_compact_len=-1))
    assert out is convo and entry["success"] is False           # unchanged, noted
    fitted = _fit_context(out, 8_000)                           # backstop still works
    assert sum(_msg_chars(m) for m in fitted) <= 8_000


# --- loop-level ------------------------------------------------------------- #

@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _answering_stream(text):
    async def stream(payload, provider=None):
        yield {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
               "usage": {"completion_tokens": 2}}
    return stream


def test_loop_auto_compact_folds_and_records_entry(home, monkeypatch):
    calls = _stub(monkeypatch, ret="LOOP-RECAP")
    monkeypatch.setattr(loop.egress, "stream", _answering_stream("done"))
    # A long multi-turn history that exceeds 80% of an 8k budget.
    mid = [_big("assistant" if i % 2 else "user", f"h{i}") for i in range(8)]
    recent = [{"role": "user", "content": f"r{i}"} for i in range(KEEP_RECENT_MSGS)]
    messages = [{"role": "user", "content": "the task"}] + mid + recent

    async def collect():
        async for _ in loop.run(messages, model="local", tools={},
                                session_id="chat:ac", system="be nice",
                                context_chars=8_000, auto_compact=True, compact_at=80):
            pass
    asyncio.run(collect())

    assert calls["n"] == 1                                       # summarizer ran once
    run = debug_db.get_session("chat:ac")["runs"][0]
    comp = [e for e in run["entries"] if e["type"] == "compaction"]
    assert len(comp) == 1 and "folded" in comp[0]["text"]
