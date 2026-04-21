import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy import text

from app.core.auth_session import _b64url_decode, _b64url_encode, get_bearer_token
from app.core.database import engine

EXTENSION_SESSION_TTL_SECONDS = int(os.getenv("EXTENSION_SESSION_TTL_SECONDS", "2592000"))


def _extension_signing_secret() -> bytes:
    root = str(os.getenv("SECRET_KEY") or "").strip()
    if not root:
        raise HTTPException(status_code=500, detail="Auth misconfigured: SECRET_KEY is required")
    return hashlib.sha256(f"ext::{root}".encode()).digest()


def hash_extension_session_secret(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode()).hexdigest()


def create_extension_access_token(*, extension_session_id: int, extension_device_id: int, user_id: str, ttl_seconds: int = EXTENSION_SESSION_TTL_SECONDS) -> str:
    now = int(time.time())
    payload = {
        "sid": int(extension_session_id),
        "did": int(extension_device_id),
        "sub": str(user_id),
        "iat": now,
        "exp": now + max(300, int(ttl_seconds)),
        "typ": "extension_access",
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_extension_signing_secret(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(signature)}"


def decode_extension_access_token(token: str) -> dict[str, Any]:
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid extension token format") from exc

    expected_sig = hmac.new(_extension_signing_secret(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(signature_b64)
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=401, detail="Invalid extension token signature") from exc

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=401, detail="Invalid extension token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode())
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid extension token payload") from exc

    if payload.get("typ") != "extension_access":
        raise HTTPException(status_code=401, detail="Invalid extension token type")
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise HTTPException(status_code=401, detail="Extension token expired")
    return payload


async def get_authenticated_extension_device(token: str | None = Depends(get_bearer_token)) -> dict[str, Any]:
    if not token:
        raise HTTPException(status_code=401, detail="Missing extension authentication")
    claims = decode_extension_access_token(token)

    async with engine.connect() as conn:
        session = (
            await conn.execute(
                text(
                    """
                SELECT es.id AS extension_session_id,
                       es.user_id,
                       es.extension_device_id,
                       es.status,
                       es.expires_at,
                       es.revoked_at,
                       ed.status AS device_status
                FROM extension_sessions es
                JOIN extension_devices ed ON ed.id = es.extension_device_id
                WHERE es.id = :sid
                """
                ),
                {"sid": claims["sid"]},
            )
        ).mappings().first()

    if not session:
        raise HTTPException(status_code=401, detail="Unknown extension session")
    if str(session["user_id"]) != str(claims["sub"]):
        raise HTTPException(status_code=401, detail="Extension user mismatch")
    if int(session["extension_device_id"]) != int(claims["did"]):
        raise HTTPException(status_code=401, detail="Extension device mismatch")
    if session["status"] != "active" or session["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="Extension session revoked")
    if session["expires_at"] is not None and session["expires_at"].timestamp() <= time.time():
        raise HTTPException(status_code=401, detail="Extension session expired")

    return {
        "extension_session_id": int(session["extension_session_id"]),
        "extension_device_id": int(session["extension_device_id"]),
        "user_id": str(session["user_id"]),
    }
