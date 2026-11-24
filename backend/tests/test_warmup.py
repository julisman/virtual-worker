"""Prompt-cache warm-up after model activation (agent/warmup.py + llama_manager)."""

import asyncio
import copy

import pytest

import config
import storage.debug_db as debug_db
from agent import crusher, loop, warmup
from agent.compose import compose
from runtime import eval_runner
from runtime import llama_manager as mgr
from storage import agent_store, chat_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _agent():
    a = agent_store.get_agent(agent_store.DEFAULT_AGENT_ID)
    assert a, "default agent should exist"
    return a


def test_warm_request_matches_loop_first_turn(home, monkeypatch):
    """The warmed prompt start must be byte-identical to a new session's first
    request: same system message, same tools in the same order."""
    agent = _agent()
    body = warmup.build_request(agent)

    sent = []

    async def fake_stream(payload, provider=None, strip_think=False):
        sent.append(copy.deepcopy(payload))
        yield "end", {"text": "hi", "tool_calls": [], "finish": "stop", "usage": {}}

    monkeypatch.setattr(loop, "_stream_turn", fake_stream)
    system, registry, *_ = compose(agent, session_id=warmup.WARMUP_SESSION_ID)

    async def run():
        async for _ in loop.run([{"role": "user", "content": "halo"}], model="local",
                                tools=registry, system=system,
                                session_id=warmup.WARMUP_SESSION_ID):
            pass

    asyncio.run(run())
    first = sent[0]
    assert first["messages"][0] == body["messages"][0]           # system prompt
    assert first.get("tools", []) == body.get("tools", [])         # tools, in order
    assert first.get("chat_template_kwargs") == body.get("chat_template_kwargs")


def test_build_request_hides_retrieve_full(home, monkeypatch):
    agent = _agent()
    real_compose = warmup.compose

    def with_retrieve(*a, **k):
        system, registry, *rest = real_compose(*a, **k)
        registry[crusher.RETRIEVE_TOOL_NAME] = {"schema": {"type": "function", "function": {
            "name": crusher.RETRIEVE_TOOL_NAME, "parameters": {"type": "object"}}}}
        return (system, registry, *rest)

    monkeypatch.setattr(warmup, "compose", with_retrieve)
    names = [t["function"]["name"] for t in warmup.build_request(agent).get("tools", [])]
    assert crusher.RETRIEVE_TOOL_NAME not in names


def test_build_request_mirrors_reasoning_off(home):
    agent = {**_agent(), "guardrails": {**(_agent().get("guardrails") or {}), "reasoning": "off"}}
    assert warmup.build_request(agent)["chat_template_kwargs"] == {"enable_thinking": False}
    assert warmup.build_request(agent)["max_tokens"] == 1


def test_recent_agent_ids_newest_first_with_fallback(home):
    assert warmup.recent_agent_ids(1) == [agent_store.DEFAULT_AGENT_ID]  # no history
    assert warmup.recent_agent_ids(0) == []
    chat_store.upsert_session("s1", "one", None, [], agent_id="agent-old")
    chat_store.upsert_session("s2", "two", None, [], agent_id="agent-new")
    chat_store.upsert_session("s3", "three", None, [], agent_id="agent-new")
    assert warmup.recent_agent_ids(1) == ["agent-new"]
    assert warmup.recent_agent_ids(5) == ["agent-new", "agent-old"]


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, str(data)

    def json(self):
        return self._data


def _fake_client(posts, code=200):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posts.append((url, json))
            return _Resp(code, {"usage": {"prompt_tokens": 9000}})
    return _C


def test_warm_posts_to_local_server(home, monkeypatch):
    posts = []
    monkeypatch.setattr(warmup.httpx, "AsyncClient", _fake_client(posts))
    res = asyncio.run(warmup.warm(8123, limit=1))
    assert res[0]["ok"] is True and res[0]["promptTokens"] == 9000
    assert posts[0][0] == "http://127.0.0.1:8123/v1/chat/completions"
    assert posts[0][1]["max_tokens"] == 1


def test_warm_skips_remote_agents_and_never_raises(home, monkeypatch):
    async def remote(agent, requested_model=None):
        return "claude", {"provider": "anthropic"}

    import gateway.providers as gp
    monkeypatch.setattr(gp, "resolve_target", remote)
    posts = []
    monkeypatch.setattr(warmup.httpx, "AsyncClient", _fake_client(posts))
    res = asyncio.run(warmup.warm(8123, limit=1))
    assert res[0]["skipped"] == "agent uses a remote provider" and not posts

    monkeypatch.undo()
    monkeypatch.setattr(config, "APP_HOME", home)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(warmup.httpx, "AsyncClient", _Boom)
    res = asyncio.run(warmup.warm(8123, limit=1))
    assert res[0]["ok"] is False and "connection refused" in res[0]["error"]


# --------------------------------------------------------------------------- #
# llama_manager wiring
# --------------------------------------------------------------------------- #

def _verify_ok(monkeypatch, warm, backend="llamacpp"):
    started = []
    monkeypatch.setattr(mgr, "_state", {**mgr._state, "pid": 1, "warm": warm, "backend": backend})
    monkeypatch.setattr(mgr, "_pid_alive", lambda pid: True)

    async def healthy(port=None):
        return True
    monkeypatch.setattr(mgr, "_healthy", healthy)
    monkeypatch.setattr(mgr.httpx, "AsyncClient", _fake_client([]))
    monkeypatch.setattr(mgr, "_start_warmup", lambda path, port: started.append(path))
    asyncio.run(mgr._run_verify("/m/model.gguf", 8123))
    assert mgr._verify["state"] == "ok"
    return started


def test_verify_ok_starts_warmup(monkeypatch):
    monkeypatch.setattr(config, "WARMUP_AGENTS", 1)
    assert _verify_ok(monkeypatch, warm=True) == ["/m/model.gguf"]


def test_no_warmup_when_disabled_or_not_requested(monkeypatch):
    monkeypatch.setattr(config, "WARMUP_AGENTS", 1)
    assert _verify_ok(monkeypatch, warm=False) == []
    assert _verify_ok(monkeypatch, warm=True, backend="mlx") == []
    monkeypatch.setattr(config, "WARMUP_AGENTS", 0)
    assert _verify_ok(monkeypatch, warm=True) == []


def test_eval_activation_skips_warmup(monkeypatch):
    seen = {}

    async def fake_start(path, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr(mgr, "start", fake_start)
    asyncio.run(eval_runner._activate("/m/model.gguf", ctx=4096))
    assert seen.get("warm") is False


def test_stop_cancels_warmup(monkeypatch):
    mgr._warmup.update(state="warming", path="/m/model.gguf")
    mgr.stop(clear_pref=False)
    assert mgr._warmup["state"] == "idle"
