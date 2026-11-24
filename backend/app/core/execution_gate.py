"""Server-side gate for live order execution. The January 2027 MVP is read-only.

TaliTrade does not currently expose any order-execution route, and no broker client sends trading
requests. This module keeps it that way at the server, independent of what the frontend shows:

* `execution_enabled()` is False by default, and is ALWAYS False in production (any Railway deployment or
  TALITRADE_ENV=production). No environment variable can turn on live trading there: enabling it requires
  a reviewed code change to this function.
* Outside production it can be switched on only with TALITRADE_ORDER_EXECUTION=enabled, for local
  development of execution features against demo accounts.
* `block_execution_routes` is HTTP middleware that answers 403 to any request whose path looks like order
  execution (an `order`/`orders`/`execute`/... path segment) while the gate is closed, so a route added
  later cannot go live by accident. `require_execution_enabled` is the per-route dependency for any future
  execution endpoint.
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.execution_gate")

FLAG = "TALITRADE_ORDER_EXECUTION"
_PRODUCTION_VALUES = {"production", "prod"}

EXECUTION_PATH = re.compile(
    r"/(orders?|place[-_]?orders?|submit[-_]?orders?|cancel[-_]?orders?|modify[-_]?orders?|"
    r"execute|execution|executions|close[-_]?positions?|open[-_]?positions?|market[-_]?orders?)(/|$)",
    re.IGNORECASE,
)

DISABLED_DETAIL = "Order execution is disabled. TaliTrade is read-only."


def _is_production() -> bool:
    if (os.getenv("TALITRADE_ENV") or "").strip().lower() in _PRODUCTION_VALUES:
        return True
    return bool((os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT") or "").strip())


def execution_enabled() -> bool:
    if _is_production():
        return False
    return (os.getenv(FLAG) or "").strip().lower() == "enabled"


def is_execution_path(path: str) -> bool:
    return bool(EXECUTION_PATH.search(path or ""))


def require_execution_enabled() -> None:
    if not execution_enabled():
        raise HTTPException(status_code=403, detail=DISABLED_DETAIL)


async def block_execution_routes(request: Request, call_next):
    match = EXECUTION_PATH.search(request.url.path or "") if request.method != "OPTIONS" else None
    if match and not execution_enabled():
        # Log the matched segment only: other path segments can carry tokens (e.g. webhook URLs).
        logger.warning("Blocked order-execution request: method=%s segment=%s", request.method, match.group(1))
        return JSONResponse(status_code=403, content={"detail": DISABLED_DETAIL})
    return await call_next(request)
