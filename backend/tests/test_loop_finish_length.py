"""A turn the provider cut at its token limit (``finish_reason == "length"``)
must be flagged to the user — a silently clipped answer is indistinguishable
from a complete one otherwise. See agent/loop.py:run."""

import asyncio

import pytest

import config
import storage.debug_db as debug_db
from agent import loop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}]}


def _stream(text, finish):
    async def stream(payload, provider=None):
        yield _chunk(delta={"content": text})
        yield _chunk(finish=finish)
    return stream


def _collect(stream, monkeypatch):
    monkeypatch.setattr(loop.egress, "stream", stream)
    events = []

    async def run():
        async for ev in loop.run([{"role": "user", "content": "q"}], model="m", tools={}):
            events.append(ev)
    asyncio.run(run())
    return events


def test_length_finish_is_flagged(home, monkeypatch):
    events = _collect(_stream("The answer is that", "length"), monkeypatch)
    text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    assert "token limit" in text            # the user sees the cut, inline
    assert events[-1]["type"] == "done"     # still a normal, recorded end


def test_clean_stop_is_not_flagged(home, monkeypatch):
    events = _collect(_stream("Done.", "stop"), monkeypatch)
    text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    assert "token limit" not in text
