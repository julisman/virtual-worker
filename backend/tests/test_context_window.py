"""PRD-23 — token-accurate context window tracking.

Covers the guardrail fields (defaults/sanitization/route round-trip), the hybrid
meter (estimate-only first call + post-compaction staleness reset), the window-mode
trigger + effective-reserve math, the window-resolution ladder (override > settings.json
> managed ctx > catalog, with unresolved → chars fallback), the compactKeep size
cut boundary, and the contextWindowInfo shape on the agents route."""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
import storage.settings as settings
from agent import context_window as cw
from agent import loop
from agent.loop import (
    KEEP_RECENT_MSGS, MAX_OUTPUT_TOKENS, _context_tokens, _estimate_tokens,
    _fold_end, _maybe_compact, _msg_chars,
)
from gateway import providers
from storage import agent_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


# --------------------------------------------------------------------------- #
# guardrails: defaults, sanitization, route round-trip (Pydantic drop trap)
# --------------------------------------------------------------------------- #

def test_new_guardrail_defaults(home):
    g = agent_store.create_agent({"name": "A"})["guardrails"]
    assert g["contextMode"] == "chars"
    assert g["reserveTokens"] == 0 and g["contextTokens"] == 0
    assert g["compactKeep"] == "messages" and g["compactKeepAmount"] == 0


def test_new_guardrail_sanitization(home):
    g = agent_store.create_agent({"name": "A", "guardrails": {
        "contextMode": "bogus", "reserveTokens": -5, "contextTokens": "nope",
        "compactKeep": "weird", "compactKeepAmount": -9}})["guardrails"]
    assert g["contextMode"] == "chars"        # bogus → default
    assert g["reserveTokens"] == 0            # negative → 0
    assert g["contextTokens"] == 0            # garbage → 0
    assert g["compactKeep"] == "messages"     # bogus → default
    assert g["compactKeepAmount"] == 0        # negative → 0


def test_new_guardrail_valid_values_stick(home):
    g = agent_store.create_agent({"name": "A", "guardrails": {
        "contextMode": "window", "reserveTokens": 8000, "contextTokens": 200000,
        "compactKeep": "size", "compactKeepAmount": 12000}})["guardrails"]
    assert g["contextMode"] == "window" and g["reserveTokens"] == 8000
    assert g["contextTokens"] == 200000
    assert g["compactKeep"] == "size" and g["compactKeepAmount"] == 12000


def test_agents_route_round_trips_new_guardrails(home):
    # Regression: guardrail sub-keys must survive PUT→GET (the Pydantic silent-drop
    # trap lives on the AgentIn model; guardrails is an open dict but assert it).
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    created = client.post("/api/agents", json={"name": "R"}).json()
    payload = {"guardrails": {"contextMode": "window", "reserveTokens": 5000,
                              "contextTokens": 131072, "compactKeep": "size",
                              "compactKeepAmount": 9000}}
    updated = client.put(f"/api/agents/{created['id']}", json=payload).json()
    for k, v in payload["guardrails"].items():
        assert updated["guardrails"][k] == v
    got = client.get(f"/api/agents/{created['id']}").json()
    for k, v in payload["guardrails"].items():
        assert got["guardrails"][k] == v


# --------------------------------------------------------------------------- #
# the meter (FR-3/4/5)
# --------------------------------------------------------------------------- #

def test_meter_estimate_only_before_usage():
    convo = [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": "y" * 800}]
    meter = {"valid": False}
    # pure chars/4 estimate of every message
    assert _context_tokens(convo, meter) == _estimate_tokens(convo[0]) + _estimate_tokens(convo[1])
    assert _context_tokens(convo, meter) == (400 // 4) + (800 // 4)


def test_meter_exact_prefix_plus_estimated_tail():
    # coveredCount=2, real usage 1000+50 tokens; a third message is the tail.
    convo = [{"role": "user", "content": "a" * 4000},
             {"role": "assistant", "content": "b" * 2000},
             {"role": "tool", "content": "c" * 800}]
    meter = {"valid": True, "promptTokens": 1000, "completionTokens": 50, "coveredCount": 2}
    assert _context_tokens(convo, meter) == 1000 + 50 + (800 // 4)


# --------------------------------------------------------------------------- #
# the trigger + effective reserve (FR-6/7)
# --------------------------------------------------------------------------- #

def test_effective_reserve_floor_and_clamp():
    # Big window: floor = MAX_OUTPUT_TOKENS + 4096, well under window//4.
    assert cw.effective_reserve(200_000, 0) == MAX_OUTPUT_TOKENS + 4096
    # A user reserve larger than the floor wins (compaction earlier).
    assert cw.effective_reserve(200_000, 50_000) == 50_000
    # Tiny window: the auto floor is clamped to window//4 so it isn't starved.
    assert cw.effective_reserve(8_000, 0) == 8_000 // 4


def _stub_summarizer(monkeypatch, ret="RECAP"):
    calls = {"n": 0}

    async def fake(model, messages, provider=None):
        calls["n"] += 1
        return ret
    monkeypatch.setattr(loop, "summarize_messages", fake)
    return calls


def _window_convo():
    mid = [{"role": "assistant" if i % 2 else "user", "content": "m" * 4000} for i in range(8)]
    tail = [{"role": "user", "content": f"r{i}"} for i in range(KEEP_RECENT_MSGS)]
    return [{"role": "system", "content": "S"}, {"role": "user", "content": "task"}] + mid + tail


def test_window_trigger_fires_above_threshold(monkeypatch):
    calls = _stub_summarizer(monkeypatch, ret="WRECAP")
    convo = _window_convo()
    # context_tokens above (window - reserve) → fires.
    out, entry, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=999_999, compact_at=80,
        last_compact_len=-1, mode="window", window=10_000, effective_reserve=2_000,
        context_tokens=9_000, compact_keep="messages", compact_keep_amount=0))
    assert calls["n"] == 1 and entry["success"] is True
    assert entry["mode"] == "window" and entry["compactsAt"] == 8_000
    assert entry["contextTokens"] == 9_000


def test_window_trigger_does_not_fire_below_threshold(monkeypatch):
    calls = _stub_summarizer(monkeypatch)
    convo = _window_convo()
    out, entry, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=999_999, compact_at=80,
        last_compact_len=-1, mode="window", window=10_000, effective_reserve=2_000,
        context_tokens=7_999, compact_keep="messages", compact_keep_amount=0))
    assert out is convo and entry is None and calls["n"] == 0


# --------------------------------------------------------------------------- #
# window resolution ladder (FR-10) — order + fallback
# --------------------------------------------------------------------------- #

def test_resolution_override_wins(home):
    w, src = cw.resolve_window_sync(model="anything", provider_id="deepseek",
                                    guardrail_context_tokens=123_456)
    assert w == 123_456 and src == "override"


def test_resolution_settings_json_map_beats_catalog(home):
    settings.set("modelWindows", {"deepseek-chat": 65_536})
    w, src = cw.resolve_window_sync(model="deepseek-chat", provider_id="deepseek",
                                    guardrail_context_tokens=0)
    assert w == 65_536 and src == "settings map"


def test_resolution_provider_catalog(home):
    w, src = cw.resolve_window_sync(model="deepseek-chat", provider_id="deepseek",
                                    guardrail_context_tokens=0)
    assert w == 131_072 and src == "catalog"
    w2, src2 = cw.resolve_window_sync(model="claude-sonnet-4-6", provider_id="anthropic",
                                      guardrail_context_tokens=0)
    assert w2 == 200_000 and src2 == "catalog"


def test_resolution_managed_llama_ctx(home, monkeypatch):
    from runtime import llama_manager
    monkeypatch.setattr(config, "MANAGED_LLAMA", True)
    monkeypatch.setitem(llama_manager._state, "backend", "llamacpp")
    monkeypatch.setitem(llama_manager._state, "ctx", 32_768)
    w, src = cw.resolve_window_sync(model="local", provider_id=None, guardrail_context_tokens=0)
    assert w == 32_768 and src == "managed server"


def test_resolution_unresolved_returns_none(home, monkeypatch):
    from runtime import llama_manager
    monkeypatch.setattr(config, "MANAGED_LLAMA", True)
    monkeypatch.setitem(llama_manager._state, "backend", None)
    monkeypatch.setitem(llama_manager._state, "ctx", None)
    monkeypatch.setitem(llama_manager._state, "path", None)
    assert cw.resolve_window_sync(model="local", provider_id=None,
                                  guardrail_context_tokens=0) == (None, None)


def test_provider_window_discovered_beats_static():
    providers._discovered_windows[("opencode-go", "glm-5.2")] = 500_000
    try:
        assert providers.provider_window("opencode-go", "glm-5.2") == (500_000, "probed")
        # static prefix fallback for an un-probed id
        assert providers.provider_window("opencode-go", "kimi-k3") == (262_144, "catalog")
    finally:
        providers._discovered_windows.pop(("opencode-go", "glm-5.2"), None)


# --------------------------------------------------------------------------- #
# compactKeep size cut (FR-20/21)
# --------------------------------------------------------------------------- #

def test_fold_end_messages_mode_no_regression():
    convo = [{"role": "user", "content": "x"} for _ in range(20)]
    # messages mode with auto amount keeps exactly KEEP_RECENT_MSGS (today's cut).
    assert _fold_end(convo, mode="chars", ctx_budget=40_000, window=None,
                     compact_keep="messages", compact_keep_amount=0) == 20 - KEEP_RECENT_MSGS
    # explicit amount overrides the count.
    assert _fold_end(convo, mode="chars", ctx_budget=40_000, window=None,
                     compact_keep="messages", compact_keep_amount=3) == 17


def test_fold_end_size_mode_fat_tail_vs_tiny_tail():
    # window mode, auto keep budget for a 200k window = clamp(20k,4k,20k)=20k tokens.
    # A fat tail (one 200k-char = 50k-token message) is cut back so the tail fits.
    fat = [{"role": "user", "content": "z"}, {"role": "user", "content": "y"},
           {"role": "user", "content": "x" * 200_000}]
    fe = _fold_end(fat, mode="window", ctx_budget=0, window=200_000,
                   compact_keep="size", compact_keep_amount=0)
    # the 50k-token last message alone exceeds the 20k budget → tail starts at it.
    assert fe == 2
    # A tiny tail: many small messages all fit under the budget → keep them all.
    tiny = [{"role": "user", "content": "hi"} for _ in range(30)]
    fe2 = _fold_end(tiny, mode="window", ctx_budget=0, window=200_000,
                    compact_keep="size", compact_keep_amount=0)
    assert fe2 == 0


def test_size_cut_preserves_pair_safety(monkeypatch):
    # size mode must still never split an assistant+tool group across the boundary.
    _stub_summarizer(monkeypatch, ret="R")
    asst = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}]}
    toolres = {"role": "tool", "tool_call_id": "t1", "content": "Z" * 8000}
    mid = [{"role": "user", "content": "m" * 4000} for _ in range(6)]
    convo = ([{"role": "system", "content": "S"}, {"role": "user", "content": "task"}]
             + mid + [asst, toolres, {"role": "user", "content": "final"}])
    out, entry, _ = asyncio.run(_maybe_compact(
        convo, model="m", provider=None, ctx_budget=0, compact_at=80,
        last_compact_len=-1, mode="window", window=8_000, effective_reserve=1_000,
        context_tokens=99_999, compact_keep="size", compact_keep_amount=0))
    ai = next(i for i, m in enumerate(out) if m.get("tool_calls"))
    assert out[ai] == asst and out[ai + 1] == toolres
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert any(out[j].get("tool_calls") for j in range(i)), "orphan tool msg"


# --------------------------------------------------------------------------- #
# loop integration: staleness reset after compaction (FR-4, R1)
# --------------------------------------------------------------------------- #

def _tool_stream(prompt_tokens=None):
    async def stream(payload, provider=None):
        chunk = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "probe", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        if prompt_tokens is not None:
            chunk["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": 3}
        yield chunk
    return stream


def _answer_stream(text):
    async def stream(payload, provider=None):
        yield {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return stream


def test_meter_invalidated_after_compaction(home, monkeypatch):
    # R1/FR-4: the meter must reset to pure-estimate after a compaction rewrite,
    # or stale usage vs the shrunken history would compact-loop forever. The
    # window-mode context debug entries show measured → (fold) → estimated again.
    _stub_summarizer(monkeypatch, ret="RECAP")

    # turn0 tool call WITH usage (→ measured); turn1 tool call WITHOUT usage (so
    # the meter is NOT re-measured before the next reading); turn2 final answer.
    seq = iter([_tool_stream(prompt_tokens=5_000), _tool_stream(None), _answer_stream("done")])

    def dispatch_stream(payload, provider=None):
        return next(seq)(payload, provider)
    monkeypatch.setattr(loop.egress, "stream", dispatch_stream)

    async def fake_tool():
        return "R" * 3000
    registry = {"probe": {"schema": {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object", "properties": {}}}},
        "fn": lambda **k: fake_tool()}}

    # A small foldable middle so the estimate-based first reading stays under the
    # trigger, but the measured 5000-token prompt at turn1's fit blows past it.
    mid = [{"role": "assistant" if i % 2 else "user", "content": "h" * 4000} for i in range(3)]
    messages = [{"role": "user", "content": "task"}] + mid

    async def collect():
        async for _ in loop.run(messages, model="local", tools=registry,
                                session_id="chat:win", system="sys",
                                auto_compact=True, context_mode="window",
                                context_window=6_000, context_window_source="managed server",
                                effective_reserve=2_000,
                                compact_keep="messages", compact_keep_amount=2):
            pass
    asyncio.run(collect())

    run = debug_db.get_session("chat:win")["runs"][0]
    comp = [e for e in run["entries"] if e["type"] == "compaction"]
    assert len(comp) == 1  # folded exactly once — no compact loop
    measured = [e["usage"]["measured"] for e in run["entries"] if e["type"] == "context"]
    assert measured[0] is False   # first reading is pure estimate (no usage yet)
    assert True in measured       # became measured once real usage arrived
    assert measured[-1] is False  # reset to estimate after the compaction fold


# --------------------------------------------------------------------------- #
# contextWindowInfo shape on the agents route (§9 / FR-18)
# --------------------------------------------------------------------------- #

def test_context_window_info_unresolved_shape(home):
    a = agent_store.create_agent({"name": "A"})  # local, no managed model → unknown
    info = cw.context_window_info(a)
    assert info == {"window": None, "source": None,
                    "effectiveReserve": None, "compactsAt": None}


def test_context_window_info_resolved_shape(home):
    a = agent_store.create_agent({"name": "A", "modelSource": "byok", "provider": "deepseek",
                                  "model": "deepseek-chat",
                                  "guardrails": {"contextMode": "window", "reserveTokens": 6000}})
    info = cw.context_window_info(a)
    assert info["window"] == 131_072 and info["source"] == "catalog"
    assert info["effectiveReserve"] == cw.effective_reserve(131_072, 6000)
    assert info["compactsAt"] == 131_072 - info["effectiveReserve"]


def test_agents_route_exposes_context_window_info(home):
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    created = client.post("/api/agents", json={
        "name": "R", "modelSource": "byok", "provider": "deepseek", "model": "deepseek-chat"}).json()
    assert set(created["contextWindowInfo"]) == {"window", "source", "effectiveReserve", "compactsAt"}
    got = client.get(f"/api/agents/{created['id']}").json()
    assert got["contextWindowInfo"]["window"] == 131_072
    listed = client.get("/api/agents").json()["agents"]
    assert all("contextWindowInfo" in a for a in listed)
