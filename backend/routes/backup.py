# Backup, Sync & Portability (PRD-11) — export/import a portable .vwbak snapshot.

"""GET /api/backup/export, POST /api/backup/import, GET /api/backup/info.

Local-first: export streams a ``.vwbak`` archive to the user's browser
download; import takes a multipart ``.vwbak`` upload and restores it over the
current studio (after writing a pre-import rollback backup). Secrets are redacted
on export by default; ``models/`` is opt-in via ``?includeModels=true`` (the HF
cache is never included).
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

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
