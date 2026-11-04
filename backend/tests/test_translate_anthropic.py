"""Anthropic <-> OpenAI translation (pure)."""

import asyncio
import json

import gateway.translate_anthropic as tr


async def _drain(agen):
    out = []
    async for b in agen:
        out.append(b)
    return out


def _parse_sse(chunks: list[bytes]) -> list[dict]:
    """Parse a list of SSE byte frames into (event, data) dicts."""
    events = []
    for raw in chunks:
        text = raw.decode("utf-8")
        ev, data = None, None
        for line in text.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append({"event": ev, "data": data})
    return events


async def _agen(items):
    for it in items:
        yield it


# --------------------------------------------------------------------------- #
# Request mapping
# --------------------------------------------------------------------------- #

def test_request_system_and_text():
    body = {"model": "claude-x", "max_tokens": 100, "system": "be brief",
            "messages": [{"role": "user", "content": "hi"}]}
    out = tr.anthropic_to_openai(body, "local")
    assert out["model"] == "local"
    assert out["max_tokens"] == 100
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_system_blocks():
    body = {"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "messages": []}
    out = tr.anthropic_to_openai(body, "local")
    assert out["messages"][0]["content"] == "ab"


def test_request_image_block():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
    ]}]}
    out = tr.anthropic_to_openai(body, "local")
    parts = out["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,AAAA")


def test_request_tool_use_and_result_roundtrip():
    body = {"messages": [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Jakarta"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "32C"},
        ]},
    ]}
    out = tr.anthropic_to_openai(body, "local")
    asst = out["messages"][0]
    assert asst["role"] == "assistant"
    assert asst["content"] == "let me check"
    assert asst["tool_calls"][0]["id"] == "toolu_1"
    assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"city": "Jakarta"}
    tool_msg = out["messages"][1]
    assert tool_msg == {"role": "tool", "tool_call_id": "toolu_1", "content": "32C"}


def test_request_tools_and_choice():
    body = {"messages": [], "tools": [
        {"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "tool", "name": "f"}}
    out = tr.anthropic_to_openai(body, "local")
    assert out["tools"][0]["function"]["name"] == "f"
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}


# --------------------------------------------------------------------------- #
# Response mapping
# --------------------------------------------------------------------------- #

def test_response_text():
    resp = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
    out = tr.openai_to_anthropic(resp, "claude-x")
    assert out["model"] == "claude-x"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 3}


def test_response_tool_use():
    resp = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_1", "function": {"name": "get_weather", "arguments": '{"city":"Jakarta"}'}}]},
        "finish_reason": "tool_calls"}]}
    out = tr.openai_to_anthropic(resp, "claude-x")
    assert out["stop_reason"] == "tool_use"
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["input"] == {"city": "Jakarta"}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

def test_stream_text_event_order():
    chunks = [
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 2}},
    ]
    frames = asyncio.run(_drain(tr.stream_anthropic(_agen(chunks), "claude-x", 7)))
    events = _parse_sse(frames)
    names = [e["event"] for e in events]
    assert names == [
        "message_start", "ping",
        "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[0]["data"]["message"]["usage"]["input_tokens"] == 7
    deltas = "".join(e["data"]["delta"]["text"] for e in events if e["event"] == "content_block_delta")
    assert deltas == "Hello"
    md = next(e for e in events if e["event"] == "message_delta")
    assert md["data"]["delta"]["stop_reason"] == "end_turn"
    assert md["data"]["usage"]["output_tokens"] == 2


def test_stream_tool_call_partial_json_concatenates():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ty":"Jakarta"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    frames = asyncio.run(_drain(tr.stream_anthropic(_agen(chunks), "claude-x", 5)))
    events = _parse_sse(frames)
    start = next(e for e in events if e["event"] == "content_block_start")
    assert start["data"]["content_block"]["type"] == "tool_use"
    assert start["data"]["content_block"]["id"] == "call_1"
    partial = "".join(
        e["data"]["delta"]["partial_json"]
        for e in events
        if e["event"] == "content_block_delta" and e["data"]["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(partial) == {"city": "Jakarta"}
    md = next(e for e in events if e["event"] == "message_delta")
    assert md["data"]["delta"]["stop_reason"] == "tool_use"


def test_estimate_tokens():
    assert tr.estimate_tokens("") == 1
    assert tr.estimate_tokens("a" * 40) == 10
