import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Header, HTTPException

DEFAULT_SESSION_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")) * 60


def _get_session_secret() -> bytes:
    raw_secret = str(os.getenv("SECRET_KEY") or "").strip()
    if not raw_secret:
        raise HTTPException(
            status_code=500,
            detail="Auth session misconfigured: SECRET_KEY is required",
        )
    return raw_secret.encode()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode())


def create_session_token(telegram_user_id: str, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> str:
    session_secret = _get_session_secret()
    now = int(time.time())
    payload = {
        "sub": str(telegram_user_id),
        "iat": now,
        "exp": now + max(60, int(ttl_seconds)),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(session_secret, payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(signature)}"


def decode_session_token(token: str) -> dict[str, Any]:
    session_secret = _get_session_secret()
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid session token format") from exc

    expected_sig = hmac.new(session_secret, payload_b64.encode(), hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(signature_b64)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid session token signature") from exc

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=401, detail="Invalid session token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode())
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid session token payload") from exc

    exp = int(payload.get("exp") or 0)
    if exp <= int(time.time()):
        raise HTTPException(status_code=401, detail="Session token expired")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid session token subject")
    return payload


def get_bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    if not authorization:
        return None
    value = authorization.strip()
    if not value.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must use Bearer token")
    token = value[7:].strip()
    return token or None
