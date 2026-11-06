from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import registry_store

router = APIRouter()


class RegistryIn(BaseModel):
    url: str
    branch: str | None = None
    subdir: str | None = None


@router.get("")
async def list_registries():
    return {"registries": registry_store.list_registries()}


@router.post("")
async def add_registry(body: RegistryIn):
    try:
        return registry_store.add_registry(body.url, body.branch, body.subdir)
    except registry_store.RegistryError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.post("/{slug}/sync")
async def sync_registry(slug: str):
    try:
        result = registry_store.sync_registry(slug)
    except registry_store.RegistryError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if result is None:
        raise HTTPException(404, "no such registry")
    return result


@router.delete("/{slug}")
async def remove_registry(slug: str):
    if registry_store.remove_registry(slug):
        return {"deleted": slug}
    raise HTTPException(404, "no such registry")
