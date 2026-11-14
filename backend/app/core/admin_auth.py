"""Authorization for administrative / maintenance endpoints.

Policy (server-side only, fails closed):

* Maintenance is OFF by default. Unless ``TALITRADE_ADMIN_MAINTENANCE_MODE=true`` the
  admin routes answer 404 exactly as if they did not exist.
* The only credential is ``TALITRADE_ADMIN_TOKEN`` (set in the server environment,
  at least 32 characters), presented in the ``X-TaliTrade-Admin-Token`` request header.
  It is never read from the query string, cookies or the request body.
* A user session is not a credential here. Telegram sessions and Telegram user ids
  never grant administrative access.

Nothing in this module logs or returns the token.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger("app.admin")

MAINTENANCE_FLAG = "TALITRADE_ADMIN_MAINTENANCE_MODE"
TOKEN_ENV = "TALITRADE_ADMIN_TOKEN"
TOKEN_HEADER = "x-talitrade-admin-token"
MIN_TOKEN_LENGTH = 32


def maintenance_enabled() -> bool:
    return str(os.getenv(MAINTENANCE_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_token() -> str:
    token = str(os.getenv(TOKEN_ENV) or "").strip()
    return token if len(token) >= MIN_TOKEN_LENGTH else ""


def admin_available() -> bool:
    """True only when maintenance is switched on AND a strong token is configured."""
    return maintenance_enabled() and bool(_configured_token())


def require_admin(request: Request) -> None:
    """FastAPI dependency guarding every administrative route."""
    operation = f"{request.method} {request.url.path}"
    if not maintenance_enabled() or not _configured_token():
        logger.warning("admin denied: %s reason=maintenance_disabled", operation)
        raise HTTPException(status_code=404, detail="Not Found")
    presented = str(request.headers.get(TOKEN_HEADER) or "").strip()
    if not presented:
        logger.warning("admin denied: %s reason=missing_credentials", operation)
        raise HTTPException(status_code=401, detail="Administrative credentials required")
    if not hmac.compare_digest(presented.encode("utf-8"), _configured_token().encode("utf-8")):
        logger.warning("admin denied: %s reason=invalid_credentials", operation)
        raise HTTPException(status_code=403, detail="Administrative credentials rejected")
    logger.warning("admin authorized: %s", operation)
