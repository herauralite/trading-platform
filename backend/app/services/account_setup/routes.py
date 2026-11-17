"""Session-authenticated routes for account setup profiles.

Same rules as the Match-Trader routes: a user session is required (extension tokens are
refused), the owner comes from the session only, and a connection the caller does not own
answers 404. The whole router answers 404 unless TALITRADE_ACCOUNT_SETUP_ENABLED is on.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import ingest_auth
from app.core.ingest_auth import Principal
from app.services.account_setup import config, schemas
from app.services.account_setup.store import Conflict, NotFound, SetupStore

logger = logging.getLogger("app.account_setup")


def require_enabled() -> None:
    if not config.enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/account-setup", tags=["account-setup"], dependencies=[Depends(require_enabled)])


def get_store() -> SetupStore:
    return SetupStore()


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/profiles")
async def list_profiles(response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """All of this trader's linked accounts with their setup profile, for the account cards."""
    _private(response)
    return {"profiles": await get_store().list_owned(principal.user_id)}


@router.get("/connections/{connection_id}")
async def read_profile(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    try:
        out = await get_store().read(connection_id, principal.user_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="Account not found") from None
    _private(response)
    return out


@router.put("/connections/{connection_id}")
async def save_profile(connection_id: int, body: schemas.SaveBody, response: Response,
                       principal: Principal = Depends(ingest_auth.require_session_principal)):
    try:
        profile = schemas.Profile.model_validate(body.profile)
    except ValidationError as exc:
        # Field locations only: the submitted values are not echoed back.
        raise HTTPException(status_code=422, detail={"message": "These answers could not be saved.",
                                                     "fields": sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})}) from None
    data = schemas.dump(profile)
    if len(json.dumps(data)) > schemas.MAX_PROFILE_BYTES:
        raise HTTPException(status_code=413, detail="These answers are too large to save.")
    try:
        saved = await get_store().save(connection_id, principal.user_id, base_version=body.base_version, save_id=body.save_id, data=data)
    except NotFound:
        raise HTTPException(status_code=404, detail="Account not found") from None
    except Conflict as conflict:
        logger.info("account setup conflict connection=%s user=%s have=%s sent=%s", connection_id, principal.user_id, conflict.version, body.base_version)
        return JSONResponse(status_code=409, headers={"Cache-Control": "no-store"}, content={
            "detail": "These answers were changed in another tab or on another device.",
            "version": conflict.version, "profile": conflict.data or None,
            "updated_at": conflict.updated_at.isoformat() if conflict.updated_at else None})
    _private(response)
    return saved
