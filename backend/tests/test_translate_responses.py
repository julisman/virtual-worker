"""OpenAI Responses <-> Chat Completions translation (pure)."""

import asyncio
import json

import gateway.translate_responses as tr


async def _drain(agen):
    return [b async for b in agen]


async def _agen(items):
    for it in items:
        yield it


def _events(frames: list[bytes]) -> list[dict]:
    out = []
    for raw in frames:
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[len("data:"):].strip()))
    return out


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #

def test_request_instructions_and_input_string():
    body = {"model": "gpt-x", "instructions": "sys", "input": "hello"}
    out = tr.responses_to_openai(body, "local")
    assert out["messages"] == [{"role": "system", "content": "sys"},
                               {"role": "user", "content": "hello"}]


def test_request_input_items_and_function_roundtrip():
    body = {"input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do it"}]},
        {"type": "function_call", "call_id": "call_1", "name": "sh", "arguments": '{"cmd":"ls"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"},
    ]}
    out = tr.responses_to_openai(body, "local")
    assert out["messages"][0] == {"role": "user", "content": "do it"}
    assert out["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert out["messages"][1]["tool_calls"][0]["function"]["name"] == "sh"
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "file.txt"}


def test_request_tools_flat_to_nested():
    body = {"input": "x", "tools": [
        {"type": "function", "name": "sh", "description": "shell", "parameters": {"type": "object"}}]}
    out = tr.responses_to_openai(body, "local")
    assert out["tools"][0] == {"type": "function", "function": {
        "name": "sh", "description": "shell", "parameters": {"type": "object"}}}


def test_request_max_output_tokens_maps():
    out = tr.responses_to_openai({"input": "x", "max_output_tokens": 256}, "local")
    assert out["max_tokens"] == 256


# --------------------------------------------------------------------------- #
# Response (non-stream)
# --------------------------------------------------------------------------- #

def test_response_text():
    resp = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}
    out = tr.openai_to_responses(resp, "gpt-x")
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"][0]["text"] == "hi"
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}


def test_response_function_call_and_incomplete():
    resp = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_9", "function": {"name": "sh", "arguments": '{"cmd":"ls"}'}}]},
        "finish_reason": "length"}]}
    out = tr.openai_to_responses(resp, "gpt-x")
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}
    fc = out["output"][0]
    assert fc["type"] == "function_call" and fc["call_id"] == "call_9" and fc["name"] == "sh"


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

def test_stream_text_sequence():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}},
    ]
    events = _events(asyncio.run(_drain(tr.stream_responses(_agen(chunks), "gpt-x"))))
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert "response.output_item.added" in types
    assert "response.content_part.added" in types
    assert types.count("response.output_text.delta") == 2
    assert "response.output_text.done" in types
    assert types[-1] == "response.completed"
    # sequence numbers monotonic
    seqs = [e["sequence_number"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    completed = events[-1]["response"]
    assert completed["output"][0]["content"][0]["text"] == "Hello"


def test_stream_function_call_arguments_aggregate():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "sh", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cm'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'd":"ls"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = _events(asyncio.run(_drain(tr.stream_responses(_agen(chunks), "gpt-x"))))
    types = [e["type"] for e in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    done = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    assert json.loads(done["arguments"]) == {"cmd": "ls"}
    completed = events[-1]["response"]
    fc = completed["output"][0]
    assert fc["type"] == "function_call" and fc["call_id"] == "call_1"
