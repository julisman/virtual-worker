"""PRD-17 — content-aware tool-result compression (crusher + CCR store +
retrieve_full). Hermetic: no model, no network; store tests run on a tmp
APP_HOME per house pattern."""

import asyncio
import json
import re

import pytest

import config
from agent import crusher
from agent.crusher import crush


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# ─── router / invariants ────────────────────────────────────────────────────

def test_passthrough_below_cap_is_byte_identical():
    text = json.dumps([{"a": 1}] * 10)
    r = crush(text, "t", cap=len(text) + 1)
    assert r.text == text and r.strategy == "passthrough"
    assert r.saved_chars == 0 and r.hash is None


def test_crush_is_deterministic():
    rows = [{"id": i, "name": f"row-{i}", "value": i * 3.7} for i in range(300)]
    text = json.dumps(rows)
    a, b = crush(text, "t", 4000), crush(text, "t", 4000)
    assert a.text == b.text and a.hash == b.hash


def test_output_respects_cap_with_slack():
    rows = [{"id": i, "blob": "x" * 500} for i in range(500)]
    text = json.dumps(rows)
    r = crush(text, "t", 6000)
    assert len(r.text) <= 6000 * 1.2 + 300
    assert r.saved_chars == len(text) - len(r.text) > 0


def test_sentinel_carries_hash_and_tool_hint():
    rows = [{"id": i, "v": i} for i in range(400)]
    r = crush(json.dumps(rows), "t", 2000)
    assert r.hash is not None
    assert f"<<vw:{r.hash} " in r.text
    assert "retrieve_full" in r.text


# ─── JSON crusher ───────────────────────────────────────────────────────────

def test_error_rows_always_survive():
    rows = [{"id": i, "status": "ok"} for i in range(200)]
    rows[137] = {"id": 137, "status": "FAILED: timeout"}
    r = crush(json.dumps(rows), "t", 3000)
    assert "FAILED: timeout" in r.text          # kept despite being mid-array
    assert r.strategy == "json"


def test_numeric_extremes_survive():
    rows = [{"id": i, "latency_ms": 100} for i in range(150)]
    rows[88]["latency_ms"] = 99999               # the max holder
    r = crush(json.dumps(rows), "t", 3000)
    assert "99999" in r.text


def test_constants_factored_out_once():
    rows = [{"env": "production", "id": i, "v": f"val{i}"} for i in range(300)]
    r = crush(json.dumps(rows), "t", 3000)
    body = r.text.rsplit("\n", 1)[0]
    assert body.count('"production"') == 1       # in constants, not per row


def test_envelope_dict_with_inner_array_is_crushed():
    # the real pis_get_api_info shape: {"data": {"data": [64 rows], ...}}
    inner = [{"api_id": str(i), "api_name": f"api-{i}", "spec": "y" * 2000}
             for i in range(64)]
    text = json.dumps({"data": {"data": inner, "retcode": 0, "message": "success"}})
    r = crush(text, "t", 6000)
    assert r.strategy == "json"
    assert '"retcode":0' in r.text.replace(" ", "")   # envelope scalars kept
    assert len(r.text) <= 6000 * 1.2 + 300


def test_big_single_object_keeps_scalar_skeleton():
    # the jira_get_issue shape: many small fields + a few huge ones
    obj = {"key": "SPLN-1", "summary": "s", "description": "d" * 50000,
           "status": {"name": "Done"}}
    r = crush(json.dumps(obj), "t", 4000)
    assert '"SPLN-1"' in r.text and '"Done"' in r.text
    assert "chars elided" in r.text              # the huge string was sliced


def test_kept_rows_carry_original_index():
    rows = [{"id": i, "payload": f"data-{i}" * 20} for i in range(50)]
    r = crush(json.dumps(rows), "t", 1500)
    assert r.strategy == "json"
    assert '"_i"' in r.text                      # rows are index-addressed


# ─── line compactor / fallback ──────────────────────────────────────────────

def test_lines_mode_dedupes_and_keeps_errors():
    lines = ["boot ok"] * 40 + ["INFO step %d" % i for i in range(300)] \
        + ["ERROR: disk full"] + ["INFO more %d" % i for i in range(300)] + ["done"]
    r = crush("\n".join(lines), "t", 3000)
    assert r.strategy == "lines"
    assert "×40" in r.text                       # dup run collapsed
    assert "ERROR: disk full" in r.text          # error line survived the middle
    assert "done" in r.text                      # tail kept


def test_headtail_keeps_both_ends():
    text = "START " + "middle " * 5000 + "END"
    r = crush(text, "t", 2000)
    assert r.text.startswith("START") and r.text.rstrip().endswith(">>")
    assert "END" in r.text


def test_malformed_json_falls_back_not_raises():
    text = '{"unclosed": [' + "x" * 10000
    r = crush(text, "t", 2000)
    assert r.strategy in ("lines", "headtail") and len(r.text) < 4000


# ─── CCR store ──────────────────────────────────────────────────────────────

def test_store_roundtrip_and_expiry(home):
    from storage import ccr_store
    h = crusher.content_hash("payload")
    ccr_store.put(h, "payload", tool_name="t", session_id="s")
    row = ccr_store.get(h)
    assert row and row["content"] == "payload" and row["toolName"] == "t"
    # expired rows are invisible and purgeable
    ccr_store.put(h, "payload", ttl_hours=-1)
    assert ccr_store.get(h) is None
    assert ccr_store.purge_expired() >= 1
    assert ccr_store.usage()["rows"] == 0


def test_store_size_cap_evicts_oldest(home, monkeypatch):
    from storage import ccr_store
    monkeypatch.setattr(ccr_store, "MAX_MB", 0.0001)  # ~100 bytes
    ccr_store.put("aaaaaaaa", "x" * 500)
    ccr_store.put("bbbbbbbb", "y" * 500)
    ccr_store.purge_expired()
    assert ccr_store.usage()["rows"] <= 1


# ─── retrieve_full tool ─────────────────────────────────────────────────────

def test_retrieve_full_roundtrip_window_and_budget(home):
    from storage import ccr_store
    original = json.dumps([{"id": i, "secret": f"s-{i}"} for i in range(2000)])
    h = crusher.content_hash(original)
    ccr_store.put(h, original)
    tool = crusher.make_retrieve_tool()[crusher.RETRIEVE_TOOL_NAME]["fn"]

    first = asyncio.run(tool(hash=h))
    assert first.startswith(original[:100])
    assert f"offset={crusher.RETRIEVE_WINDOW_CHARS}" in first    # paging hint
    page2 = asyncio.run(tool(hash=h, offset=crusher.RETRIEVE_WINDOW_CHARS))
    assert original[crusher.RETRIEVE_WINDOW_CHARS:crusher.RETRIEVE_WINDOW_CHARS + 50] in page2

    assert "needs the hex hash" in asyncio.run(tool(hash="nope"))
    assert "no stored original" in asyncio.run(tool(hash="deadbeef"))
    for _ in range(crusher.RETRIEVE_BUDGET_PER_RUN):
        asyncio.run(tool(hash=h))
    assert "retrieve budget reached" in asyncio.run(tool(hash=h))


def test_retrieve_full_query_jumps_to_match(home):
    from storage import ccr_store
    original = json.dumps([{"id": i, "note": f"row {i}"} for i in range(3000)])
    original = original.replace('"row 2222"', '"NEEDLE-XYZ here"')
    h = crusher.content_hash(original)
    ccr_store.put(h, original)
    tool = crusher.make_retrieve_tool()[crusher.RETRIEVE_TOOL_NAME]["fn"]

    hit = asyncio.run(tool(hash=h, query="NEEDLE-XYZ"))
    assert "NEEDLE-XYZ here" in hit and hit.startswith("[match at char")
    miss = asyncio.run(tool(hash=h, query="not-there-at-all"))
    assert "not found" in miss


# ─── loop integration ───────────────────────────────────────────────────────

def _big_json_tool():
    rows = [{"id": i, "status": "ok", "note": f"n{i}" * 30} for i in range(400)]
    rows[250] = {"id": 250, "status": "error: quota exceeded", "note": "boom"}
    payload = json.dumps(rows)

    async def fn() -> str:
        return payload
    schema = {"type": "function", "function": {
        "name": "get_rows", "description": "rows",
        "parameters": {"type": "object", "properties": {}}}}
    return payload, {"get_rows": {"schema": schema, "fn": fn}}


def _tool_call_stream(name):
    """One turn that calls `name`, then a closing answer turn."""
    calls = {"n": 0}

    async def stream(payload, provider=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": name, "arguments": "{}"}}]},
                "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return stream


def _run_loop(monkeypatch, registry, **kw):
    from agent import loop
    monkeypatch.setattr(loop.egress, "stream", _tool_call_stream(next(iter(registry))))
    events = []

    async def collect():
        async for ev in loop.run([{"role": "user", "content": "go"}], model="m",
                                 tools=registry, session_id="chat:crush", **kw):
            events.append(ev)
    asyncio.run(collect())
    return events


def test_loop_crushes_and_stores_original(home, monkeypatch):
    import storage.debug_db as debug_db
    from storage import ccr_store
    monkeypatch.setattr(debug_db, "_schema_ready", False, raising=False)
    payload, registry = _big_json_tool()

    _run_loop(monkeypatch, registry)

    run = debug_db.get_session("chat:crush")["runs"][0]
    tr = next(e for e in run["entries"] if e["type"] == "tool_result")
    assert tr["toolResult"] == payload           # trace keeps the FULL result
    assert tr["truncatedChars"] > 0 and tr["crushStrategy"] == "json"
    h = crusher.content_hash(payload)
    stored = ccr_store.get(h)
    assert stored and stored["content"] == payload   # original retrievable
    assert stored["toolName"] == "get_rows"


def test_loop_compression_off_restores_legacy(home, monkeypatch):
    import storage.debug_db as debug_db
    from storage import ccr_store
    monkeypatch.setattr(debug_db, "_schema_ready", False, raising=False)
    payload, registry = _big_json_tool()

    _run_loop(monkeypatch, registry, compression=False)

    run = debug_db.get_session("chat:crush")["runs"][0]
    tr = next(e for e in run["entries"] if e["type"] == "tool_result")
    assert tr["crushStrategy"] is None
    assert ccr_store.get(crusher.content_hash(payload)) is None  # nothing stored


def test_compose_injects_retrieve_tool_unless_disabled(home):
    from agent.compose import compose
    _, registry, *_ = compose({"id": "a1", "persona": "p"})
    assert crusher.RETRIEVE_TOOL_NAME in registry
    _, registry_off, *_ = compose(
        {"id": "a1", "persona": "p", "guardrails": {"compression": "off"}})
    assert crusher.RETRIEVE_TOOL_NAME not in registry_off


# ─── retrieve_full is gated on marker presence ───────────────────────────────

def _capturing_stream(tool_name=None):
    """Stream that records the tool-name list sent on each turn. If ``tool_name``
    is given, turn 1 calls it (so a big result gets crushed), then answers; else
    it answers immediately on turn 1."""
    turns: list[list[str]] = []

    async def stream(payload, provider=None):
        turns.append([f["function"]["name"] for f in payload.get("tools", [])])
        if tool_name and len(turns) == 1:
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": tool_name, "arguments": "{}"}}]},
                "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return stream, turns


def _retrieve_registry():
    schema = {"type": "function", "function": {
        "name": "noop", "description": "noop",
        "parameters": {"type": "object", "properties": {}}}}

    async def fn() -> str:
        return "ok"
    registry = {"noop": {"schema": schema, "fn": fn}}
    registry.update(crusher.make_retrieve_tool())
    return registry


def _run_with_stream(monkeypatch, registry, messages, stream, **kw):
    from agent import loop
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ev in loop.run(messages, model="m", tools=registry,
                                  session_id="chat:gate", **kw):
            pass
    asyncio.run(collect())


def test_retrieve_full_hidden_without_marker(home, monkeypatch):
    import storage.debug_db as debug_db
    monkeypatch.setattr(debug_db, "_schema_ready", False, raising=False)
    stream, turns = _capturing_stream()
    _run_with_stream(monkeypatch, _retrieve_registry(),
                     [{"role": "user", "content": "hello, no markers here"}], stream)
    assert turns and crusher.RETRIEVE_TOOL_NAME not in turns[0]


def test_retrieve_full_shown_when_marker_in_history(home, monkeypatch):
    import storage.debug_db as debug_db
    monkeypatch.setattr(debug_db, "_schema_ready", False, raising=False)
    stream, turns = _capturing_stream()
    _run_with_stream(monkeypatch, _retrieve_registry(),
                     [{"role": "user", "content": "recall <<vw:a1b2c3d4e5f60718 · 9 omitted>>"}],
                     stream)
    assert turns and crusher.RETRIEVE_TOOL_NAME in turns[0]


def test_retrieve_full_appears_after_a_crush(home, monkeypatch):
    import storage.debug_db as debug_db
    monkeypatch.setattr(debug_db, "_schema_ready", False, raising=False)
    _, big_registry = _big_json_tool()
    registry = dict(big_registry)
    registry.update(crusher.make_retrieve_tool())
    stream, turns = _capturing_stream(tool_name="get_rows")
    _run_with_stream(monkeypatch, registry,
                     [{"role": "user", "content": "go"}], stream)
    # Turn 1 has no marker yet; turn 2 runs after get_rows was crushed into a
    # <<vw:…>> marker, so the tool becomes visible.
    assert crusher.RETRIEVE_TOOL_NAME not in turns[0]
    assert crusher.RETRIEVE_TOOL_NAME in turns[1]


def test_retrieve_full_description_has_no_literal_hash_example(home):
    schema = crusher.make_retrieve_tool()[crusher.RETRIEVE_TOOL_NAME]["schema"]
    desc = schema["function"]["description"]
    assert not re.search(r"[0-9a-f]{16}", desc)
    assert "do not call this tool" in desc.lower()
