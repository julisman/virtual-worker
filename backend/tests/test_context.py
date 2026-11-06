"""Context management — per-result cap + conversation budget trim (loop.py)."""

from agent.loop import (
    MAX_TOOL_RESULT_CHARS, MCP_TOOL_RESULT_CHARS, KEEP_RECENT_MSGS,
    _IMAGE_CONTEXT_CHARS, _cap, _fit_context, _msg_chars,
)


def test_cap_truncates_over_limit():
    assert _cap("short", 100) == "short"
    out = _cap("x" * 500, 100)
    assert out.startswith("x" * 100) and "truncated" in out and len(out) < 500


def test_mcp_results_get_a_larger_cap_than_general_tools():
    # MCP tool results (large JSON) must keep more than the small general cap, so
    # a field past the general limit still reaches the model.
    assert MCP_TOOL_RESULT_CHARS > MAX_TOOL_RESULT_CHARS
    big = "y" * (MAX_TOOL_RESULT_CHARS + 5000)
    assert "truncated" in _cap(big, MAX_TOOL_RESULT_CHARS)   # general tool: cut
    assert _cap(big, MCP_TOOL_RESULT_CHARS) == big           # MCP: kept whole


def test_msg_chars_uses_bounded_allowance_for_inline_image_data():
    encoded = "A" * 1_400_000
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Please inspect this screenshot"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ],
    }

    assert _msg_chars(message) == len("Please inspect this screenshot") + _IMAGE_CONTEXT_CHARS


def test_inline_image_does_not_create_false_context_overflow():
    encoded = "A" * 1_400_000
    convo = [{
        "role": "user",
        "content": [{"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{encoded}"}}],
    }]

    assert sum(_msg_chars(message) for message in convo) < 10_000


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
    # Everything bulky and inside the recent window: protected TOOL results are
    # shrunk (not dropped) as a LAST resort so the budget still means something;
    # the final message is never touched (harness review batch 2).
    convo = [{"role": "tool", "tool_call_id": str(i), "content": "Y" * 3000} for i in range(KEEP_RECENT_MSGS)]
    fitted = _fit_context(convo, 100)
    assert "Y" * 3000 in fitted[-1]["content"]                       # final message intact
    assert all("elided to fit" in m["content"] for m in fitted[:-1])  # rest shrunk, not lost
