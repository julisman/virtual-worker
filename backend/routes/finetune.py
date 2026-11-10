"""Local LoRA fine-tuning (MLX, Apple Silicon) — the Train page's API.

  GET    /api/finetune/capability          Apple Silicon? mlx-lm / mlx-vlm installed? setup state
  POST   /api/finetune/setup               install mlx-lm via the engines installer (background)
  GET    /api/finetune/base-models         on-disk models, flagged trainable or not
  POST   /api/finetune/datasets            upload a JSONL dataset (multipart "file")
  POST   /api/finetune/datasets/feedback   build a dataset from Chat feedback (up + edited turns)
  GET    /api/finetune/datasets            list datasets
  DELETE /api/finetune/datasets/{id}       delete a dataset
  POST   /api/finetune/jobs                start a training job (one at a time)
  GET    /api/finetune/jobs                list jobs
  GET    /api/finetune/jobs/{id}           one job (status, metrics, log tail)
  GET    /api/finetune/jobs/{id}/stream    SSE: snapshot, then log / metric / status / done
  POST   /api/finetune/jobs/{id}/cancel    stop a running job
  POST   /api/finetune/jobs/{id}/activate  serve the result as the on-device model
  DELETE /api/finetune/jobs/{id}           delete a finished job and its files

Admin-only in team mode (it installs packages, holds the GPU and swaps the model).
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from runtime import finetune_manager as ft
from storage import feedback_store

router = APIRouter()

_MAX_UPLOAD = 200 * 1024 * 1024


@router.get("/capability")
async def capability():
    return await asyncio.to_thread(ft.capability)


@router.post("/setup")
async def setup():
    try:
        return await ft.setup()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/base-models")
async def base_models():
    return {"models": await asyncio.to_thread(ft.list_base_models)}


@router.post("/datasets")
async def upload_dataset(file: UploadFile):
    raw = await file.read(_MAX_UPLOAD + 1)
    if len(raw) > _MAX_UPLOAD:
        raise HTTPException(413, "dataset is larger than 200 MB")
    try:
        return await asyncio.to_thread(ft.save_dataset, file.filename or "dataset.jsonl", raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/datasets/feedback")
async def dataset_from_feedback(request: Request):
    body = await request.json() if await request.body() else {}
    agent_id = (body or {}).get("agentId") or None
    try:
        raw, summary = feedback_store.build_dataset(agent_id=agent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = str((body or {}).get("name") or f"feedback-{stamp}").strip()
    if not name.endswith(".jsonl"):
        name += ".jsonl"
    rec = ft.save_dataset(name, raw, source="feedback")
    return {**rec, **{k: summary[k] for k in ("positives", "corrections", "skipped")}}


@router.get("/datasets")
async def datasets():
    return {"datasets": ft.list_datasets()}


@router.delete("/datasets/{did}")
async def delete_dataset(did: str):
    if not ft.delete_dataset(did):
        raise HTTPException(404, "no such dataset")
    return {"deleted": did}


@router.post("/jobs")
async def start(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    try:
        return await ft.start(body)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs")
async def jobs():
    return {"jobs": ft.list_jobs()}


@router.get("/jobs/{job_id}")
async def job(job_id: str):
    j = ft.get_job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j


@router.get("/jobs/{job_id}/stream")
async def stream(job_id: str):
    j = ft.get_job(job_id)
    if not j:
        raise HTTPException(404, "no such job")

    async def gen():
        q = ft.subscribe(job_id)
        try:
            snap = {"type": "snapshot", "status": j.get("status"), "metrics": j.get("metrics", []),
                    "logTail": j.get("logTail", []), "progress": j.get("progress"),
                    "outputPath": j.get("outputPath"), "error": j.get("error")}
            yield f"data: {json.dumps(snap)}\n\n".encode()
            while True:
                if j.get("status") in ft.FINISHED and q.empty():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
        finally:
            ft.unsubscribe(job_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    try:
        return await ft.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, "no such job") from exc


@router.post("/jobs/{job_id}/activate")
async def activate(job_id: str):
    try:
        return await ft.activate(job_id)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    try:
        return ft.delete(job_id)
    except KeyError as exc:
        raise HTTPException(404, "no such job") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
