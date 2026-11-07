# Backup, Sync & Portability (PRD-11) — export/import a portable .vwbak snapshot.

"""GET /api/backup/export, POST /api/backup/import, GET /api/backup/info,
GET/PUT /api/backup/schedule, POST /api/backup/schedule/run,
GET/DELETE /api/backup/snapshots/{name}.

Local-first: export streams a ``.vwbak`` archive to the user's browser
download; import takes a multipart ``.vwbak`` upload and restores it over the
current studio (after writing a pre-import rollback backup). Secrets are redacted
on export by default; ``models/`` is opt-in via ``?includeModels=true`` (the HF
cache is never included).
"""

import logging
from datetime import datetime, timezone

import asyncio

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from storage import backup

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/info")
async def info():
    return backup.last_backup_info()


@router.get("/export")
async def export(includeModels: bool = False, redactSecrets: bool = True):
    try:
        data, manifest = backup.export_backup(
            include_models=includeModels, redact_secrets=redactSecrets
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean 500 to the UI
        logger.exception("backup export failed")
        raise HTTPException(500, f"export failed: {exc}") from exc

    backup.mark_exported()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"studio-{stamp}.vwbak"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Vesbak-Manifest": "manifest.json",
        },
    )


@router.post("/import")
async def import_(file: UploadFile = File(...)):
    name = file.filename or "backup.vwbak"
    if not name.lower().endswith(".vwbak"):
        raise HTTPException(400, "upload a .vwbak archive")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        result = backup.import_backup(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("backup import failed")
        raise HTTPException(500, f"import failed: {exc}") from exc
    return result


# --------------------------------------------------------------------------- #
# Scheduled local snapshots (FR-X3)
# --------------------------------------------------------------------------- #

class ScheduleIn(BaseModel):
    enabled: bool | None = None
    intervalHours: int | None = None
    keep: int | None = None
    includeModels: bool | None = None
    redactSecrets: bool | None = None


@router.get("/schedule")
async def get_schedule():
    return backup.get_schedule()


@router.put("/schedule")
async def set_schedule(body: ScheduleIn):
    return backup.set_schedule(body.model_dump(exclude_none=True))


@router.post("/schedule/run")
async def run_schedule_now():
    """Write a scheduled-style snapshot to ``<home>/backups`` right now."""
    result = await asyncio.to_thread(backup.scheduled_backup_once, force=True)
    if not result.get("ran"):
        raise HTTPException(500, f"backup failed: {result.get('error') or result.get('reason')}")
    result.pop("manifest", None)
    return result


@router.get("/snapshots/{name}")
async def download_snapshot(name: str):
    path = backup.snapshot_path(name)
    if not path:
        raise HTTPException(404, "no such snapshot")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.delete("/snapshots/{name}")
async def delete_snapshot(name: str):
    path = backup.snapshot_path(name)
    if not path:
        raise HTTPException(404, "no such snapshot")
    path.unlink()
    return {"ok": True}
