# Managed runtime — start/stop/switch the active local model (llama.cpp).

"""GET /api/runtime/status, POST /api/runtime/start, POST /api/runtime/stop.

Lets the UI pick which on-device GGUF is the active model. Switching stops the
running llama-server and starts a new one on the chosen file.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from runtime import llama_manager as mgr

logger = logging.getLogger(__name__)

router = APIRouter()


class StartRequest(BaseModel):
    path: str                 # absolute path to a .gguf on disk
    ctx: int | None = None
    ngl: int | None = None
    force: bool = False        # take over the port from an existing llama-server
    mtp: bool = False          # llama.cpp: self-speculate off the embedded MTP head
    mtpNMax: int | None = None  # MTP draft depth (default 3)


@router.get("/status")
async def status():
    return await mgr.status()


@router.post("/start")
async def start(req: StartRequest):
    try:
        kw = {} if req.mtpNMax is None else {"mtp_n_max": req.mtpNMax}
        return await mgr.start(req.path, ctx=req.ctx, ngl=req.ngl, force=req.force,
                               mtp=req.mtp, **kw)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/stop")
async def stop():
    return mgr.stop()
