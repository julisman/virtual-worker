"""Context management — per-result cap + conversation budget trim (loop.py)."""

from agent.loop import KEEP_RECENT_MSGS, _cap, _fit_context, _msg_chars


def test_cap_truncates_over_limit():
    assert _cap("short", 100) == "short"
    out = _cap("x" * 500, 100)
    assert out.startswith("x" * 100) and "truncated" in out and len(out) < 500


def test_fit_context_noop_under_budget():
    convo = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert _fit_context(convo, 10_000) is convo  # unchanged identity


def test_fit_context_trims_old_bulky_turns():
    big = "Z" * 5000
    convo = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "the original task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": big},      # old + bulky -> should elide
        {"role": "assistant", "content": "thinking " + big},        # old + bulky -> should elide
    ] + [{"role": "tool", "tool_call_id": str(i), "content": f"recent {i}"} for i in range(KEEP_RECENT_MSGS)]
    fitted = _fit_context(convo, 5_000)
    total = sum(_msg_chars(m) for m in fitted)
    assert total <= 5_000
    # system + the original task are preserved verbatim
    assert fitted[0]["content"] == "SYSTEM"
    assert fitted[1]["content"] == "the original task"
    # the old bulky tool result got elided
    assert "elided" in fitted[3]["content"]
    # a recent message is protected (kept intact)
    assert fitted[-1]["content"].startswith("recent")


def test_fit_context_protects_recent_window():
    # Everything bulky but within the recent window -> can't trim below, returns best effort.
    convo = [{"role": "tool", "tool_call_id": str(i), "content": "Y" * 3000} for i in range(KEEP_RECENT_MSGS)]
    fitted = _fit_context(convo, 100)
    # recent messages are protected, so they survive even over budget (no crash, no data loss)
    assert all("Y" * 3000 in m["content"] for m in fitted)
