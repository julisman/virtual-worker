"""Evaluation suites API (PRD §8.14).

  GET    /api/eval/suites                    list suites
  POST   /api/eval/suites                    create {name, description, items}
  POST   /api/eval/suites/import             create from an exported suite JSON
  POST   /api/eval/suites/from-feedback      build a judge-scored suite from feedback
  GET    /api/eval/suites/{id}               one suite
  PUT    /api/eval/suites/{id}               update (a changed case list bumps the version)
  DELETE /api/eval/suites/{id}
  GET    /api/eval/suites/{id}/export        portable JSON
  GET    /api/eval/templates                 starter cases per task type
  POST   /api/eval/runs                      start {suiteId, models[], options}
  GET    /api/eval/runs                      list
  GET    /api/eval/runs/{id}                 run + progress + leaderboard
  GET    /api/eval/runs/{id}/stream          SSE live progress
  GET    /api/eval/runs/{id}/leaderboard
  GET    /api/eval/runs/{id}/results         per (model, case, repetition)
  GET    /api/eval/runs/{id}/matrix          per case, models side by side
  GET    /api/eval/runs/{id}/export          results as JSON or CSV (?format=csv)
  POST   /api/eval/runs/{id}/cancel
  DELETE /api/eval/runs/{id}
  POST   /api/eval/runs/{id}/promote         activate a model from the run
"""

import asyncio
import csv
import io
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from runtime import eval_runner
from storage import eval_store

logger = logging.getLogger(__name__)

router = APIRouter()


class SuiteBody(BaseModel):
    name: str = "Untitled"
    description: str = ""
    items: list[dict] = []


class FromFeedbackBody(BaseModel):
    agentId: str | None = None
    name: str | None = None


@router.get("/suites")
async def list_suites():
    return {"suites": eval_store.list_suites()}


@router.post("/suites")
async def create_suite(body: SuiteBody):
    return eval_store.create_suite(body.name, body.description, body.items)


@router.post("/suites/import")
async def import_suite(request: Request):
    try:
        data = await request.json()
        return eval_store.import_suite(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/suites/from-feedback")
async def from_feedback(body: FromFeedbackBody):
    try:
        return eval_store.suite_from_feedback(agent_id=body.agentId, name=body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/suites/{suite_id}")
async def get_suite(suite_id: str):
    s = eval_store.get_suite(suite_id)
    if not s:
        raise HTTPException(404, "no such suite")
    return s


@router.put("/suites/{suite_id}")
async def update_suite(suite_id: str, body: SuiteBody):
    s = eval_store.update_suite(suite_id, name=body.name, description=body.description, items=body.items)
    if not s:
        raise HTTPException(404, "no such suite")
    return s


@router.delete("/suites/{suite_id}")
async def delete_suite(suite_id: str):
    if not eval_store.delete_suite(suite_id):
        raise HTTPException(404, "no such suite")
    return {"deleted": suite_id}


@router.get("/suites/{suite_id}/export")
async def export_suite(suite_id: str):
    data = eval_store.export_suite(suite_id)
    if not data:
        raise HTTPException(404, "no such suite")
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in data["name"])[:60] or "suite"
    return Response(content=json.dumps(data, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.eval.json"'})


_TEMPLATES = [
    {"taskType": "qa", "label": "Q&A (exact answer)",
     "item": {"prompt": "What is the capital of France?", "expected": "Paris", "taskType": "qa"}},
    {"taskType": "summarization", "label": "Summarization (keywords)",
     "item": {"prompt": "Summarize in one sentence: The mitochondria is the powerhouse of the cell.",
              "rubric": ["mitochondria", "cell"], "taskType": "summarization"}},
    {"taskType": "tool_use", "label": "Tool call (function calling)",
     "item": {"prompt": "What's the weather in Paris right now?", "taskType": "tool_use",
              "tools": [{"type": "function", "function": {
                  "name": "get_weather", "description": "Current weather for a city",
                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
              "expectedTool": {"name": "get_weather", "args": {"city": "Paris"}}}},
    {"taskType": "coding", "label": "Coding (keyword check)",
     "item": {"prompt": "Write a Python one-liner that returns the sum of a list `xs`.", "rubric": ["sum"],
              "taskType": "coding"}},
    {"taskType": "freeform", "label": "Judged (LLM grader)",
     "item": {"prompt": "Explain why local-first AI matters, in two sentences.", "taskType": "freeform",
              "judge": "Give full marks for two clear sentences that mention privacy or control and speed or cost."}},
]


@router.get("/templates")
async def templates():
    return {"templates": _TEMPLATES}


class RunBody(BaseModel):
    suiteId: str
    models: list[dict] = []
    options: dict = {}


@router.post("/runs")
async def start_run(body: RunBody):
    if eval_runner.active_runs():
        raise HTTPException(409, "a benchmark is already running; wait for it or cancel it")
    try:
        run = eval_store.create_run(body.suiteId, body.models, body.options)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    eval_runner.start_run(run["id"])
    return run


@router.get("/runs")
async def list_runs():
    return {"runs": eval_store.list_runs()}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    run["leaderboard"] = eval_store.leaderboard(run_id)
    return run


@router.get("/runs/{run_id}/leaderboard")
async def leaderboard(run_id: str):
    if not eval_store.get_run(run_id):
        raise HTTPException(404, "no such run")
    return {"leaderboard": eval_store.leaderboard(run_id)}


@router.get("/runs/{run_id}/results")
async def results(run_id: str):
    if not eval_store.get_run(run_id):
        raise HTTPException(404, "no such run")
    return {"results": eval_store.list_results(run_id)}


@router.get("/runs/{run_id}/matrix")
async def matrix(run_id: str):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    suite = eval_store.get_suite(run["suiteId"]) or {"items": []}
    return {"cases": suite["items"], "matrix": eval_store.case_matrix(run_id)}


@router.get("/runs/{run_id}/export")
async def export_results(run_id: str, format: str = "json"):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    rows = eval_store.list_results(run_id)
    if format == "csv":
        buf = io.StringIO()
        cols = ["model", "itemIndex", "rep", "scorer", "score", "scored", "latencyMs", "promptTokens",
                "completionTokens", "tokensPerSec", "rssBytes", "error", "rationale", "output"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="eval-{run_id}.csv"'})
    body = {"run": run, "leaderboard": eval_store.leaderboard(run_id), "results": rows}
    return Response(content=json.dumps(body, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="eval-{run_id}.json"'})


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    if not eval_store.get_run(run_id):
        raise HTTPException(404, "no such run")
    return {"canceled": eval_runner.cancel_run(run_id)}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    if run["status"] in ("running", "pending"):
        raise HTTPException(409, "run is still active; cancel it first")
    eval_store.delete_run(run_id)
    return {"deleted": run_id}


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")

    async def gen():
        snap = {"type": "snapshot", "status": run["status"], "progress": run.get("progress"),
                "leaderboard": eval_store.leaderboard(run_id)}
        yield f"data: {json.dumps(snap)}\n\n".encode()
        q = eval_runner.subscribe(run_id)
        try:
            while True:
                cur = eval_store.get_run(run_id)
                if (not cur or cur["status"] not in ("pending", "running")) and q.empty():
                    final = {"type": "done", "status": cur["status"] if cur else "error",
                             "leaderboard": eval_store.leaderboard(run_id)}
                    yield f"data: {json.dumps(final)}\n\n".encode()
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
        finally:
            eval_runner.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


class PromoteBody(BaseModel):
    path: str | None = None
    model: str | None = None
    ctx: int | None = None


@router.post("/runs/{run_id}/promote")
async def promote(run_id: str, body: PromoteBody):
    run = eval_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    path = body.path
    if not path and body.model:
        for m in run["models"]:
            if (m.get("label") or m.get("name") or m.get("path")) == body.model:
                path = m.get("path")
                break
    if not path:
        raise HTTPException(400, "a model path (or a model label from this run) is required")
    if eval_runner.active_runs():
        raise HTTPException(409, "wait for the running benchmark to finish before promoting")
    from runtime import llama_manager
    try:
        return await llama_manager.start(path, ctx=body.ctx, force=True)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
