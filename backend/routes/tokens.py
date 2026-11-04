# API access tokens — create/list/revoke the keys agents use for the gateway.

"""GET /api/tokens, POST /api/tokens, DELETE /api/tokens/{id}.

The gateway is open (any key accepted) until the first token is created; after
that a valid token is required. Plaintext is returned only once, at creation.
NOTE: localhost only — these management endpoints are unauthenticated like the
rest of /api/*; don't expose the backend beyond localhost without adding auth.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import token_store

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateTokenRequest(BaseModel):
    name: str = "token"
    ttlDays: int | None = None  # None = never expires


@router.get("")
async def list_tokens():
    return {"tokens": token_store.list_tokens()}


@router.post("")
async def create_token(req: CreateTokenRequest):
    if req.ttlDays is not None and req.ttlDays <= 0:
        raise HTTPException(400, "ttlDays must be positive or null")
    return token_store.create(req.name, req.ttlDays)


@router.delete("/{token_id}")
async def revoke_token(token_id: str):
    if not token_store.revoke(token_id):
        raise HTTPException(404, "no such token")
    return {"deleted": token_id}
