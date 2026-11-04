# Inference engines — versions + updates (llama.cpp, mlx-lm, mlx-vlm, mlx).

"""GET /api/engines (installed, local), GET /api/engines/check (latest, network),
POST /api/engines/{key}/update (run the update)."""

import logging

from fastapi import APIRouter, HTTPException

from runtime import engines

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_engines():
    return {"engines": engines.current()}


@router.get("/check")
async def check_updates():
    return {"engines": await engines.check()}


@router.post("/{key}/update")
async def update_engine(key: str):
    if not engines._by_key(key):
        raise HTTPException(404, f"unknown engine: {key}")
    return await engines.update(key)
