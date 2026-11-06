# Skills — upload/manage agentskills.io SKILL.md packs (local-first).

"""GET /api/skills, POST /api/skills (upload .md/.zip), GET/DELETE /api/skills/{slug}.

Skills are stored on disk under <VW_HOME>/skills/<slug>/. Agents attach
them by slug; the full body loads on demand via the load_skill tool.
"""

import logging

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from storage import skill_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_skills():
    return {"skills": skill_store.list_skills()}


@router.post("")
async def upload_skill(file: UploadFile = File(...)):
    name = file.filename or "skill.md"
    if not name.lower().endswith((".md", ".zip", ".markdown")):
        raise HTTPException(400, "upload a .md or .zip file")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        skill = skill_store.save_upload(name, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return skill


@router.get("/builtin")
async def list_builtin_skills():
    """Skill packs shipped with the app (repo ``skills/`` / bundled), each with
    category, dependency, and installed state. Read-only — never mutates."""
    return {"skills": skill_store.list_builtin()}


@router.post("/install/{slug}")
async def install_builtin_skill(slug: str):
    """Install a builtin pack (plus its ``requires_skills`` closure) into the
    Virtual Worker skills library. Returns the installed slugs + any required CLI tools
    missing from PATH + domain grants for the consent notice."""
    try:
        return skill_store.install_builtin(slug)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/update-all")
async def update_all_builtin_skills():
    """Re-install every installed builtin pack whose shipped copy has drifted
    (``updateAvailable``). Up-to-date packs are left untouched. Returns the
    updated slugs + any required CLI tools missing from PATH + domain grants.
    """
    return skill_store.update_all_builtin()


@router.get("/{slug}")
async def get_skill(slug: str):
    s = skill_store.get_skill(slug)
    if not s:
        raise HTTPException(404, "no such skill")
    return s


@router.get("/{slug}/export")
async def export_skill(slug: str):
    """Download the skill as a ``.zip`` bundle (round-trips through upload)."""
    data = skill_store.export_zip(slug)
    if data is None:
        raise HTTPException(404, "no such skill")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


@router.delete("/{slug}")
async def delete_skill(slug: str):
    if not skill_store.delete_skill(slug):
        raise HTTPException(404, "no such skill")
    return {"deleted": slug}
