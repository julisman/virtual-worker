# Skills — upload/manage agentskills.io SKILL.md packs (local-first).

"""GET /api/skills, POST /api/skills (upload .md/.zip), GET/DELETE /api/skills/{slug}.

Skills are stored on disk under <VW_HOME>/skills/<slug>/. Agents attach
them by slug; the full body loads on demand via the load_skill tool.
"""

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

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


@router.get("/{slug}")
async def get_skill(slug: str):
    s = skill_store.get_skill(slug)
    if not s:
        raise HTTPException(404, "no such skill")
    return s


@router.delete("/{slug}")
async def delete_skill(slug: str):
    if not skill_store.delete_skill(slug):
        raise HTTPException(404, "no such skill")
    return {"deleted": slug}
