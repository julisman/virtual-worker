"""Evaluation suites: store, scorers (exact, rubric, tool, judge), the runner
end to end over stubbed runtime seams, leaderboard, import/export, suites
from feedback, and the routes. No model is loaded."""

import asyncio
import json

import pytest

import config
from runtime import eval_runner
from storage import eval_store, feedback_store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    eval_runner._TASKS.clear()
    eval_runner._CANCEL.clear()


# ─── store ───────────────────────────────────────────────────────────────────

def test_suite_crud_versions_and_normalization():
    s = eval_store.create_suite("My suite", "desc", items=[
        {"prompt": "Capital of France?", "expected": "Paris"},
        {"prompt": "  "},
        {"prompt": "weather", "tools": [{"type": "function", "function": {"name": "get_weather"}}],
         "expectedTool": {"name": "get_weather", "args": {"city": "Paris"}}},
        {"prompt": "judged", "judge": "be strict", "scorer": "bogus"},
        {"prompt": "kw", "rubric": "alpha, beta ,gamma"},
    ])
    assert s["id"].startswith("es_") and s["version"] == 1 and len(s["items"]) == 4
    assert [eval_store.effective_scorer(c) for c in s["items"]] == ["exact", "tool", "judge", "rubric"]
    assert s["items"][3]["rubric"] == ["alpha", "beta", "gamma"] and s["items"][2]["scorer"] == "auto"
    same = eval_store.update_suite(s["id"], name="Renamed", items=s["items"])
    assert same["name"] == "Renamed" and same["version"] == 1          # unchanged cases: no bump
    upd = eval_store.update_suite(s["id"], items=[{"prompt": "2+2?", "expected": "4"}])
    assert upd["version"] == 2 and upd["items"][0]["prompt"] == "2+2?"
    assert eval_store.list_suites()[0]["id"] == s["id"]
    assert eval_store.delete_suite(s["id"]) is True and eval_store.get_suite(s["id"]) is None


def test_export_import_roundtrip():
    s = eval_store.create_suite("Port", "d", items=[{"prompt": "p", "expected": "a"}])
    data = eval_store.export_suite(s["id"])
    assert data["format"] == "virtual-worker-eval-suite" and data["items"][0]["expected"] == "a"
    imported = eval_store.import_suite(json.loads(json.dumps(data)))
    assert imported["id"] != s["id"] and imported["name"] == "Port" and imported["items"] == s["items"]
    with pytest.raises(ValueError):
        eval_store.import_suite({"items": [{"prompt": ""}]})


def test_suite_from_feedback():
    feedback_store.record({"sessionId": "s", "messageId": "1", "signal": "up", "userPrompt": "Greet me", "original": "Hello!"})
    feedback_store.record({"sessionId": "s", "messageId": "2", "signal": "edit", "userPrompt": "Capital?",
                           "original": "Berlin", "correctedText": "Paris"})
    feedback_store.record({"sessionId": "s", "messageId": "3", "signal": "down", "userPrompt": "x", "original": "y"})
    s = eval_store.suite_from_feedback()
    assert len(s["items"]) == 2
    by = {c["prompt"]: c for c in s["items"]}
    assert by["Capital?"]["expected"] == "Paris" and by["Greet me"]["expected"] == "Hello!"
    assert all(eval_store.effective_scorer(c) == "judge" for c in s["items"])


def test_options_normalized_and_run_requires_models():
    s = eval_store.create_suite("s", items=[{"prompt": "p"}])
    with pytest.raises(ValueError):
        eval_store.create_run(s["id"], models=[])
    run = eval_store.create_run(s["id"], models=[{"path": "/m/a"}],
                                options={"temperature": 9, "maxTokens": "x", "repetitions": 99, "judge": "self"})
    assert run["options"] == {"temperature": 2.0, "maxTokens": 512, "repetitions": 5,
                              "judge": {"mode": "self", "provider": "", "model": ""}}
    assert run["progress"]["itemTotal"] == 5 and run["suiteVersion"] == 1


# ─── scorers ─────────────────────────────────────────────────────────────────

def test_exact_and_rubric_scoring():
    assert eval_runner.score_item("The capital is paris.", {"expected": "Paris"}) == (1.0, True)
    assert eval_runner.score_item("London", {"expected": "Paris"}) == (0.0, True)
    score, scored = eval_runner.score_item("the cell mitochondria glows", {"rubric": ["mitochondria", "cell", "nucleus"]})
    assert scored and score == pytest.approx(2 / 3, abs=1e-4)
    assert eval_runner.score_item("anything", {"prompt": "p"}) == (None, False)
    assert eval_runner.score_item("anything", {"prompt": "p", "judge": "x"}) == (None, False)   # judged later


def test_tool_scoring():
    exp = {"name": "get_weather", "args": {"city": "Paris"}}
    assert eval_runner.score_tool([{"name": "get_weather", "args": {"city": "paris", "units": "c"}}], exp)[0] == 1.0
    s, why = eval_runner.score_tool([{"name": "get_weather", "args": {"units": "c"}}], exp)
    assert s == 0.0 and "missing city" in why
    s, why = eval_runner.score_tool([{"name": "search", "args": {}}], exp)
    assert s == 0.0 and "instead of get_weather" in why
    assert eval_runner.score_tool([], exp) == (0.0, "no tool call was made")
    item = {"prompt": "w", "expectedTool": exp}
    assert eval_runner.score_item("", item, tool_calls=[{"name": "get_weather", "args": {"city": "Paris"}}]) == (1.0, True)


def test_judge_parsing():
    assert eval_runner.parse_judge('{"score": 0.8, "rationale": "good"}') == (0.8, "good")
    assert eval_runner.parse_judge('Sure! {"score": 7, "rationale": "ok"}')[0] == 0.7
    assert eval_runner.parse_judge("Score: 1")[0] == 1.0
    sc, why = eval_runner.parse_judge("no number here")
    assert sc is None and "could not be parsed" in why
    msgs = eval_runner.judge_prompt({"prompt": "Q", "expected": "A", "judge": "strict"}, "out")
    assert msgs[0]["role"] == "system" and "Reference answer:\nA" in msgs[1]["content"]


# ─── runner end to end ───────────────────────────────────────────────────────

def _patch_runtime(monkeypatch, answers: dict, *, fail_models: set | None = None, tool_calls: dict | None = None,
                   judge_reply: str = '{"score": 0.5, "rationale": "meh"}'):
    fail_models = fail_models or set()
    activated = {"path": None}
    restored = {"called": False, "snapshot": "unset"}
    judged = []

    async def fake_snapshot():
        return {"path": "/prev/model.gguf", "ctx": 4096}

    async def fake_activate(path, ctx=None):
        activated["path"] = path
        return {"running": True}

    async def fake_wait_healthy(timeout=0):
        return activated["path"] not in fail_models

    async def fake_active_id():
        return activated["path"]

    async def fake_chat(payload):
        path = activated["path"]
        idx = fake_chat.counter.setdefault(path, 0)
        fake_chat.counter[path] = idx + 1
        text = answers.get(path, {}).get(idx, "")
        msg = {"content": text}
        if payload.get("tools") and tool_calls and path in tool_calls:
            msg["content"] = None
            msg["tool_calls"] = [{"function": {"name": n, "arguments": json.dumps(a)}} for n, a in tool_calls[path]]
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    fake_chat.counter = {}

    async def fake_judge(messages, judge, model_id):
        judged.append((judge, model_id))
        return judge_reply

    async def fake_restore(snapshot):
        restored["called"] = True
        restored["snapshot"] = snapshot

    monkeypatch.setattr(eval_runner, "_snapshot_active", fake_snapshot)
    monkeypatch.setattr(eval_runner, "_activate", fake_activate)
    monkeypatch.setattr(eval_runner, "_wait_healthy", fake_wait_healthy)
    monkeypatch.setattr(eval_runner, "_active_model_id", fake_active_id)
    monkeypatch.setattr(eval_runner, "_chat_completion", fake_chat)
    monkeypatch.setattr(eval_runner, "_judge_completion", fake_judge)
    monkeypatch.setattr(eval_runner, "_rss_bytes", lambda: 123456789)
    monkeypatch.setattr(eval_runner, "_restore", fake_restore)
    return restored, judged


def test_run_scores_builds_leaderboard_and_restores(monkeypatch):
    suite = eval_store.create_suite("cap", items=[
        {"prompt": "Capital of France?", "expected": "Paris"},
        {"prompt": "Capital of Japan?", "expected": "Tokyo"},
    ])
    run = eval_store.create_run(suite["id"], models=[{"path": "/m/good", "label": "good"}, {"path": "/m/bad", "label": "bad"}])
    restored, _ = _patch_runtime(monkeypatch, answers={"/m/good": {0: "Paris", 1: "Tokyo"}, "/m/bad": {0: "Paris", 1: "Berlin"}})
    asyncio.run(eval_runner.run_eval(run["id"]))
    assert eval_store.get_run(run["id"])["status"] == "done"
    results = eval_store.list_results(run["id"])
    assert len(results) == 4 and all(r["scored"] and r["tokens"] == 10 and r["rssBytes"] == 123456789 for r in results)
    lb = eval_store.leaderboard(run["id"])
    assert lb[0]["model"] == "good" and lb[0]["winner"] and lb[0]["avgScore"] == 1.0 and lb[0]["peakMemoryBytes"] == 123456789
    assert lb[1]["model"] == "bad" and lb[1]["avgScore"] == 0.5
    assert restored["called"] and restored["snapshot"]["path"] == "/prev/model.gguf"
    matrix = eval_store.case_matrix(run["id"])
    assert matrix[1]["models"]["bad"]["output"] == "Berlin" and matrix[1]["models"]["bad"]["score"] == 0.0


def test_run_tool_and_judge_cases_with_repetitions(monkeypatch):
    suite = eval_store.create_suite("mixed", items=[
        {"prompt": "weather in Paris", "tools": [{"type": "function", "function": {"name": "get_weather"}}],
         "expectedTool": {"name": "get_weather", "args": {"city": "Paris"}}},
        {"prompt": "explain", "judge": "be strict"},
        {"prompt": "unjudged", "judge": "x"},
    ])
    run = eval_store.create_run(suite["id"], models=[{"path": "/m/a", "label": "a"}],
                                options={"repetitions": 2, "judge": {"mode": "self"}})
    _, judged = _patch_runtime(monkeypatch, answers={"/m/a": {1: "text", 2: "text", 4: "t", 5: "t"}},
                               tool_calls={"/m/a": [("get_weather", {"city": "Paris"})]},
                               judge_reply='{"score": 0.75, "rationale": "fine"}')
    asyncio.run(eval_runner.run_eval(run["id"]))
    results = eval_store.list_results(run["id"])
    assert len(results) == 6 and {r["rep"] for r in results} == {0, 1}
    tool = [r for r in results if r["scorer"] == "tool"]
    assert all(r["score"] == 1.0 and r["toolCalls"][0]["name"] == "get_weather" for r in tool)
    assert tool[0]["output"].startswith("→ get_weather(")
    judge = [r for r in results if r["scorer"] == "judge"]
    assert all(r["score"] == 0.75 and r["rationale"] == "fine" for r in judge) and len(judged) == 4
    lb = eval_store.leaderboard(run["id"])[0]
    assert lb["toolReliability"] == 1.0 and lb["toolCases"] == 2 and lb["judgeScore"] == 0.75 and lb["cases"] == 6
    assert eval_store.get_run(run["id"])["progress"]["itemTotal"] == 6


def test_run_without_judge_leaves_judge_cases_unscored(monkeypatch):
    suite = eval_store.create_suite("j", items=[{"prompt": "p", "judge": "x"}])
    run = eval_store.create_run(suite["id"], models=[{"path": "/m/a", "label": "a"}])
    _, judged = _patch_runtime(monkeypatch, answers={"/m/a": {0: "out"}})
    asyncio.run(eval_runner.run_eval(run["id"]))
    r = eval_store.list_results(run["id"])[0]
    assert not r["scored"] and "no judge configured" in r["rationale"] and judged == []


def test_failed_activation_marks_all_cases_and_cancel(monkeypatch):
    suite = eval_store.create_suite("s", items=[{"prompt": "p1", "expected": "a"}, {"prompt": "p2", "expected": "a"}])
    run = eval_store.create_run(suite["id"], models=[{"path": "/m/ok", "label": "ok"}, {"path": "/m/dead", "label": "dead"}])
    _patch_runtime(monkeypatch, answers={"/m/ok": {0: "a", 1: "a"}}, fail_models={"/m/dead"})
    asyncio.run(eval_runner.run_eval(run["id"]))
    dead = [r for r in eval_store.list_results(run["id"]) if r["model"] == "dead"]
    assert len(dead) == 2 and all(r["error"] == "model failed to activate" for r in dead)
    lb = eval_store.leaderboard(run["id"])
    assert next(r for r in lb if r["model"] == "ok")["winner"]
    assert next(r for r in lb if r["model"] == "dead")["avgScore"] is None
    # cancel: flag before running → no model gets past activation
    run2 = eval_store.create_run(suite["id"], models=[{"path": "/m/ok", "label": "ok"}])
    assert eval_runner.cancel_run(run2["id"]) is True
    asyncio.run(eval_runner.run_eval(run2["id"]))
    assert eval_store.get_run(run2["id"])["status"] == "canceled" and eval_store.list_results(run2["id"]) == []
    assert eval_store.mark_stale_runs() == 0


def test_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    _patch_runtime(monkeypatch, answers={"/m/a": {0: "Paris"}})
    with TestClient(app) as c:
        assert len(c.get("/api/eval/templates").json()["templates"]) == 5
        s = c.post("/api/eval/suites", json={"name": "S", "items": [{"prompt": "Capital of France?", "expected": "Paris"}]}).json()
        exp = c.get(f"/api/eval/suites/{s['id']}/export")
        assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
        imp = c.post("/api/eval/suites/import", content=exp.content, headers={"content-type": "application/json"})
        assert imp.status_code == 200 and imp.json()["items"] == s["items"]
        assert c.post("/api/eval/suites/from-feedback", json={}).status_code == 400
        assert c.post("/api/eval/runs", json={"suiteId": s["id"], "models": []}).status_code == 400
        r = c.post("/api/eval/runs", json={"suiteId": s["id"], "models": [{"path": "/m/a", "label": "a"}]})
        assert r.status_code == 200
        rid = r.json()["id"]
        import time
        for _ in range(100):
            run = c.get(f"/api/eval/runs/{rid}").json()
            if run["status"] not in ("pending", "running"):
                break
            time.sleep(0.05)
        assert run["status"] == "done" and run["leaderboard"][0]["avgScore"] == 1.0
        assert c.get(f"/api/eval/runs/{rid}/matrix").json()["matrix"][0]["models"]["a"]["output"] == "Paris"
        csv_out = c.get(f"/api/eval/runs/{rid}/export?format=csv")
        assert csv_out.status_code == 200 and csv_out.text.splitlines()[0].startswith("model,itemIndex")
        assert c.delete(f"/api/eval/runs/{rid}").status_code == 200
        assert c.get(f"/api/eval/runs/{rid}").status_code == 404
