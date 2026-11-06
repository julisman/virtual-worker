import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from gateway import egress, providers
from gateway.upstream import UpstreamError
from storage import chat_store, reasoning_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


class _FakeResp:
    def __init__(self, lines, status=200, body=None):
        self.status_code = status
        self._lines = lines
        self._body = body or b"upstream error body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body


class _FakeClient:
    response = None

    def __init__(self, *args, **kwargs):
        pass

    def stream(self, method, url, json=None, headers=None):
        return self.response

    async def aclose(self):
        pass


class _SequenceClient:
    responses = []
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def stream(self, method, url, json=None, headers=None):
        self.sent.append({"method": method, "url": url, "json": json.copy(),
                          "headers": headers})
        return self.responses.pop(0)

    async def aclose(self):
        pass


def _drain(agen):
    async def run():
        return [chunk async for chunk in agen]

    return asyncio.run(run())


def _sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False)


TARGET = {
    "provider": "openai", "kind": "codex", "base_url": "https://x",
    "api_key": "k", "account_id": "acc",
}


def test_responses_capture_provider_items_allowlist_merge_and_order(monkeypatch):
    _FakeClient.response = _FakeResp([
        _sse({"type": "response.output_item.done", "output_index": 2,
              "item": {"type": "function_call", "id": "call", "call_id": "call",
                        "name": "lookup", "arguments": "{}"}}),
        _sse({"type": "response.output_item.done", "output_index": 1,
              "item": {"type": "reasoning", "id": "r1", "encrypted_content": "enc",
                        "summary": [{"type": "summary_text", "text": "first"}]}}),
        _sse({"type": "response.output_item.done", "output_index": 0,
              "item": {"type": "message", "id": "m1", "content": []}}),
        _sse({"type": "response.output_item.done", "output_index": 3,
              "item": {"type": "web_search", "id": "hosted"}}),
        _sse({"type": "response.output_item.done", "output_index": 4,
              "item": {"type": "custom_tool_call", "id": "custom"}}),
        _sse({"type": "response.output_item.done", "output_index": 5,
              "item": {"type": "unknown_item", "id": "unknown"}}),
        _sse({"type": "response.output_item.done", "output_index": 1,
              "item": {"type": "reasoning", "id": "r1", "encrypted_content": "enc",
                        "summary": [{"type": "summary_text", "text": "second"}]}}),
        _sse({"type": "response.completed", "response": {"usage": {}}}),
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)

    chunks = _drain(egress.stream({"model": "m", "messages": []}, TARGET))
    items = chunks[-1]["providerItems"]
    assert [item["type"] for item in items] == ["message", "reasoning", "function_call"]
    assert [part["text"] for part in items[1]["summary"]] == ["first", "second"]
    assert all(item["type"] not in {"web_search", "custom_tool_call", "unknown_item"}
               for item in items)
    assert "providerItems" not in chunks[0]


def test_responses_backfills_encrypted_reasoning_and_drops_declined(monkeypatch):
    _FakeClient.response = _FakeResp([
        _sse({"type": "response.output_item.done", "item":
              {"type": "reasoning", "id": "backfill", "summary": []}}),
        _sse({"type": "response.output_item.done", "item":
              {"type": "reasoning", "id": "declined", "encrypted_content": None}}),
        _sse({"type": "response.completed", "response": {"usage": {}, "output": [
            {"type": "reasoning", "id": "backfill", "encrypted_content": "from-output"},
            {"type": "reasoning", "id": "declined"},
        ]}}),
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)

    chunks = _drain(egress.stream({"model": "m", "messages": []}, TARGET))
    assert chunks[-1]["providerItems"] == [
        {"type": "reasoning", "id": "backfill", "summary": [],
         "encrypted_content": "from-output"},
    ]


def test_reasoning_sidecar_store_round_trip_preserves_prior_rows_and_isolated(home):
    items = [{"type": "reasoning", "encrypted_content": "opaque"}]
    reasoning_store.put("s", 1, "f1", items, provider="openai", model="m", origin_id="acc")
    reasoning_store.put("s", 5, "f5", items, provider="openai", model="m", origin_id="acc")
    # A later short run must not infer that the earlier high-sequence row is
    # stale: sequence numbers are conversation-local, not a retention signal.
    reasoning_store.put("s", 2, "f2", items, provider="openai", model="m", origin_id="acc")
    rows = reasoning_store.get_for_session("s")
    assert [row["seq"] for row in rows] == [1, 2, 5]
    assert rows[0]["items"] == items and rows[0]["originId"] == "acc"

    chat_store.upsert_session("s", "title", "m", [{"role": "user", "content": "hi"}])
    debug_db.insert_run({
        "run_id": "run", "session_id": "s", "start_time": "now",
        "created_at": "now", "status_code": 200,
    }, [])
    assert "reasoning_sidecars" not in repr(chat_store.get_session("s"))
    assert "reasoning_sidecars" not in repr(debug_db.get_run("run"))
    assert reasoning_store.delete_session("s") == 3
    assert reasoning_store.get_for_session("s") == []


def test_incomplete_turn_does_not_persist_reasoning_sidecar(home, monkeypatch):
    async def failed_stream(payload, provider=None):
        raise UpstreamError(502, "failed")
        yield  # keep this an async generator

    monkeypatch.setattr(loop.egress, "stream", failed_stream)

    async def run():
        return [event async for event in loop.run(
            [{"role": "user", "content": "hi"}], model="m", session_id="s",
            provider={"provider": "openai", "kind": "codex", "base_url": "https://x",
                      "api_key": "k", "account_id": "acc"})]

    events = asyncio.run(run())
    assert any(event["type"] == "error" for event in events)
    assert reasoning_store.get_for_session("s") == []


def test_clean_terminal_turn_persists_reasoning_sidecar(home, monkeypatch):
    provider_items = [
        {"type": "reasoning", "encrypted_content": "x", "id": "r1"},
        {"type": "message", "id": "m1", "content": []},
    ]

    async def clean_stream(payload, provider=None):
        yield {"choices": [{"delta": {"content": "answer"}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "providerItems": provider_items,
        }

    monkeypatch.setattr(loop.egress, "stream", clean_stream)
    session_id = "clean-session"
    model = "remote-model"
    provider = {
        "provider": "openai", "model": model, "account_id": "acc",
        "kind": "codex", "base_url": "https://x", "api_key": "k",
    }

    async def run():
        return [event async for event in loop.run(
            [{"role": "user", "content": "hi"}], model=model,
            session_id=session_id, provider=provider)]

    events = asyncio.run(run())
    assert any(event["type"] == "done" for event in events)
    rows = reasoning_store.get_for_session(session_id)
    assert len(rows) == 1
    assistant = {"role": "assistant", "content": "answer", "tool_calls": []}
    assert rows[0]["seq"] == 1
    assert rows[0]["fingerprint"] == reasoning_store.sidecar_fingerprint(assistant)
    assert rows[0]["items"] == provider_items
    assert rows[0]["provider"] == "openai"
    assert rows[0]["model"] == model
    assert rows[0]["originId"] == "acc"


def test_same_run_sidecar_is_replayed_on_next_model_request(home, monkeypatch):
    provider_items = [
        {"type": "reasoning", "id": "r1", "encrypted_content": "opaque"},
        {"type": "message", "id": "m1", "content": []},
    ]
    requests = []
    calls = {"n": 0}

    async def two_turn_stream(payload, provider=None):
        requests.append(payload.copy())
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"choices": [{"delta": {"content": "checking"}}]}
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "ping", "arguments": "{}"},
            }]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                   "providerItems": provider_items}
        else:
            yield {"choices": [{"delta": {"content": "done"}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 1}}

    async def ping(**kwargs):
        return "pong"

    monkeypatch.setattr(loop.egress, "stream", two_turn_stream)
    registry = {"ping": {"schema": {"type": "function", "function": {
        "name": "ping", "description": "ping", "parameters": {
            "type": "object", "properties": {},
        }}}, "fn": ping}}
    provider = {
        "provider": "openai-compatible", "model": "m", "kind": "responses",
        "base_url": "https://gateway.example/v1", "api_key": "k",
        "account_id": "acc", "stateless_reasoning_replay": True,
    }

    async def run():
        return [event async for event in loop.run(
            [{"role": "user", "content": "check"}], model="m",
            session_id="same-run-replay", tools=registry, provider=provider)]

    events = asyncio.run(run())
    assert any(event["type"] == "done" for event in events)
    assert len(requests) == 2
    sidecars = requests[1]["_reasoning_sidecars"]
    assert sidecars[1]["items"] == provider_items

    responses_payload = egress._to_responses_request(requests[1].copy(), provider)
    assert any(item.get("type") == "reasoning" for item in responses_payload["input"])
    assert all("summary" in item for item in responses_payload["input"]
               if item.get("type") == "reasoning")


def test_per_turn_usage_and_run_cache_columns(home, monkeypatch):
    # A multi-turn remote run: each assistant turn reports its own usage (with a
    # cacheRead). Every assistant Debug entry must carry that turn's usage, and
    # the run row must sum the per-turn cache reads/writes (camelCase on read).
    calls = {"n": 0}

    async def two_turn_stream(payload, provider=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"choices": [{"delta": {"content": "let me check"}}]}
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "ping", "arguments": "{}"}}]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                             "cacheRead": 100, "cacheWrite": 5}}
        else:
            yield {"choices": [{"delta": {"content": "done"}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1200, "completion_tokens": 8,
                             "cacheRead": 200, "cacheWrite": 0}}

    monkeypatch.setattr(loop.egress, "stream", two_turn_stream)

    async def ping(**kwargs):
        return "pong"

    registry = {"ping": {"schema": {"type": "function", "function": {
        "name": "ping", "description": "ping the server",
        "parameters": {"type": "object", "properties": {}}}}, "fn": ping}}
    provider = {"provider": "openai", "model": "m", "kind": "codex",
                "base_url": "https://x", "api_key": "k", "account_id": "acc"}

    async def run():
        return [event async for event in loop.run(
            [{"role": "user", "content": "hi"}], model="m",
            session_id="cache-turns", tools=registry, provider=provider)]

    events = asyncio.run(run())
    assert any(e["type"] == "done" for e in events)

    run_row = debug_db.get_session("cache-turns")["runs"][0]
    # (a) usage attached to BOTH assistant entries, each with its own cacheRead.
    assistant_usages = [e["usage"] for e in run_row["entries"]
                        if e["type"] == "assistant" and e.get("usage")]
    assert len(assistant_usages) == 2
    assert assistant_usages[0]["cacheRead"] == 100
    assert assistant_usages[1]["cacheRead"] == 200
    assert assistant_usages[0]["input"] == 1000
    assert assistant_usages[0]["totalTokens"] == 1010
    # (b) run-level cache columns summed across turns.
    assert run_row["cacheRead"] == 300 and run_row["cacheWrite"] == 5
    # (c) exposed camelCase on the serialized run.
    assert "cacheRead" in run_row


def test_reasoningless_terminal_turn_does_not_persist_sidecar(home, monkeypatch):
    # A responses-shaped gateway in front of a model that returns no reasoning
    # (only message/function_call items) must not write a dead-weight sidecar.
    provider_items = [
        {"type": "message", "id": "m1", "content": []},
        {"type": "function_call", "id": "c1", "call_id": "c1",
         "name": "lookup", "arguments": "{}"},
    ]

    async def clean_stream(payload, provider=None):
        yield {"choices": [{"delta": {"content": "answer"}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "providerItems": provider_items,
        }

    monkeypatch.setattr(loop.egress, "stream", clean_stream)
    provider = {"provider": "openai-compatible", "model": "m", "kind": "responses",
                "base_url": "https://x", "api_key": "k"}

    async def run():
        return [event async for event in loop.run(
            [{"role": "user", "content": "hi"}], model="m",
            session_id="reasoningless", provider=provider)]

    events = asyncio.run(run())
    assert any(event["type"] == "done" for event in events)
    assert reasoning_store.get_for_session("reasoningless") == []


def test_responses_replays_verbatim_atomically_and_fails_closed():
    assistant = {"role": "assistant", "content": "answer", "tool_calls": [
        {"id": "call-1", "type": "function",
         "function": {"name": "lookup", "arguments": '{"q":"x"}'}}
    ]}
    messages = [
        {"role": "user", "content": "hi"},
        assistant,
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    model = "gpt-5.6-luna"
    target = {"provider": "openai", "account_id": "acc"}
    replay_items = [
        {"type": "reasoning", "id": "r1", "encrypted_content": "opaque"},
        {"type": "message", "id": "m1", "content": []},
    ]
    row = {"items": replay_items,
           "fingerprint": reasoning_store.sidecar_fingerprint(assistant),
           "provider": "openai", "model": model, "originId": "acc"}
    body = egress._to_responses_request(
        {"model": model, "messages": messages, "_reasoning_sidecars": {1: row}}, target)
    plain = egress._to_responses_request({"model": model, "messages": messages}, target)
    # Sidecars are stored with ``summary`` stripped; the API requires the field
    # on reasoning input items, so replay restores it as empty.
    sent_items = [{**replay_items[0], "summary": []}, replay_items[1]]
    assert body["input"] == [plain["input"][0], *sent_items, plain["input"][-1]]
    assert replay_items[0] == {"type": "reasoning", "id": "r1",
                               "encrypted_content": "opaque"}  # row not mutated
    assert body["_reasoning_replay_plain_input"] == plain["input"]
    assert "_reasoning_sidecars" not in body

    baseline = egress._to_responses_request({"model": model, "messages": messages}, target)
    assert baseline == plain and "_reasoning_replay_plain_input" not in baseline
    for altered in [
        {**row, "fingerprint": "wrong"},
        {**row, "model": "other-model"},
        {**row, "originId": "other-account"},
        {**row, "items": "not-a-list"},
        {**row, "items": [replay_items[0], "not-a-dict"]},
    ]:
        fallback = egress._to_responses_request(
            {"model": model, "messages": messages, "_reasoning_sidecars": {1: altered}},
            target)
        assert fallback["input"] == baseline["input"]
        assert "_reasoning_replay_plain_input" not in fallback


def test_responses_replay_4xx_retries_plain_input_without_mutating_store(home, monkeypatch):
    assistant = {"role": "assistant", "content": "answer", "tool_calls": []}
    messages = [{"role": "user", "content": "hi"}, assistant]
    model = "gpt-5.6-luna"
    target = {**TARGET, "model": model}
    items = [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}]
    reasoning_store.put("retry", 1, reasoning_store.sidecar_fingerprint(assistant), items,
                        provider="openai", model=model, origin_id="acc")
    before = reasoning_store.get_for_session("retry")
    payload = {"model": model, "messages": messages, "_reasoning_sidecars": {1: {
        "items": items, "fingerprint": before[0]["fingerprint"], "provider": "openai",
        "model": model, "originId": "acc"}}}
    plain_input = egress._to_responses_request(
        {"model": model, "messages": messages}, target)["input"]
    _SequenceClient.sent = []
    _SequenceClient.responses = [
        _FakeResp([], status=400, body=b"invalid encrypted_content in reasoning"),
        _FakeResp([_sse({"type": "response.completed", "response": {"usage": {}}})]),
    ]
    monkeypatch.setattr(egress.httpx, "AsyncClient", _SequenceClient)
    _drain(egress.stream(payload, target))
    assert len(_SequenceClient.sent) == 2
    assert _SequenceClient.sent[1]["json"]["input"] == plain_input
    assert all("_reasoning_replay_plain_input" not in sent["json"]
               for sent in _SequenceClient.sent)
    assert reasoning_store.get_for_session("retry") == before

    _SequenceClient.sent = []
    _SequenceClient.responses = [_FakeResp([], status=400, body=b"invalid request")]
    with pytest.raises(UpstreamError):
        _drain(egress.stream(payload, target))
    assert len(_SequenceClient.sent) == 1


def test_responses_replay_sends_summary_and_retries_on_summary_400(home, monkeypatch):
    """Sidecar rows persist with ``summary`` stripped; replay must restore it as
    an empty list (the Responses API rejects reasoning input items without it),
    and a summary-shaped 400 must still fall back to plain input."""
    assistant = {"role": "assistant", "content": "answer", "tool_calls": []}
    messages = [{"role": "user", "content": "hi"}, assistant]
    model = "gpt-5.6-luna"
    target = {**TARGET, "model": model}
    items = [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}]
    payload = {"model": model, "messages": messages, "_reasoning_sidecars": {1: {
        "items": items, "fingerprint": reasoning_store.sidecar_fingerprint(assistant),
        "provider": "openai", "model": model, "originId": "acc"}}}
    plain_input = egress._to_responses_request(
        {"model": model, "messages": messages}, target)["input"]
    _SequenceClient.sent = []
    _SequenceClient.responses = [
        _FakeResp([], status=400,
                  body=b"Missing required parameter: 'input[21].summary'."),
        _FakeResp([_sse({"type": "response.completed", "response": {"usage": {}}})]),
    ]
    monkeypatch.setattr(egress.httpx, "AsyncClient", _SequenceClient)
    _drain(egress.stream(payload, target))
    replayed = [item for item in _SequenceClient.sent[0]["json"]["input"]
                if item.get("type") == "reasoning"]
    assert replayed == [{"type": "reasoning", "id": "r1",
                         "encrypted_content": "opaque", "summary": []}]
    assert len(_SequenceClient.sent) == 2
    assert _SequenceClient.sent[1]["json"]["input"] == plain_input


def test_private_reasoning_keys_stay_out_of_chat_and_anthropic_translators():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
               "_reasoning_sidecars": {0: {"items": [{"encrypted_content": "secret"}]}}}
    chat = egress._openai_body(payload, {"provider": "openai"})
    anthropic = egress._to_anthropic_request(payload, {"auth_mode": "apikey"})
    assert "_reasoning_sidecars" not in chat and "secret" not in repr(chat)
    assert "_reasoning_sidecars" not in anthropic and "secret" not in repr(anthropic)


def test_session_delete_removes_reasoning_sidecars(home):
    from routes.sessions import delete_session

    chat_store.upsert_session("delete-me", "title", "m", [{"role": "user", "content": "hi"}])
    reasoning_store.put("delete-me", 1, "fingerprint", [{"type": "reasoning"}],
                        provider="openai", model="m", origin_id="acc")
    assert asyncio.run(delete_session("delete-me")) == {"deleted": "delete-me"}
    assert reasoning_store.get_for_session("delete-me") == []
