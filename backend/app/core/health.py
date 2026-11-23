"""Liveness / readiness state for the web process.

/health        liveness: the process is up and serving HTTP. No dependencies are touched, so a slow or
               unreachable database never makes the platform restart a healthy process.
/health/ready  readiness: startup finished every schema step and the database answers `SELECT 1` within
               a short timeout. Use this as the deploy health check.

Responses never include exception text, host names, or connection strings: failures are reported as a
fixed status word, and the detail goes to the server log as an exception type only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import text

logger = logging.getLogger(__name__)

READINESS_DB_TIMEOUT_SECONDS = 3.0


class StartupState:
    def __init__(self) -> None:
        self.complete = False
        self.failed_step: str | None = None

    def reset(self) -> None:
        self.complete = False
        self.failed_step = None


startup_state = StartupState()


async def run_startup_step(name: str, step: Callable[[], Awaitable[Any]]) -> Any:
    """Run one startup step, logging its outcome without leaking exception text.

    A failing step is re-raised: a web process with a half-created schema must not report ready."""
    try:
        result = await step()
    except Exception as exc:
        startup_state.failed_step = name
        logger.error("Startup step failed: step=%s exception_type=%s", name, type(exc).__name__)
        raise
    logger.info("Startup step ok: step=%s", name)
    return result


def supervise_task(task: asyncio.Task, name: str) -> asyncio.Task:
    """Log (type only) when a background task dies, instead of losing the exception silently."""
    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("Background task stopped with an error: task=%s exception_type=%s", name, type(exc).__name__)
    task.add_done_callback(_done)
    return task


async def check_database(engine, timeout: float = READINESS_DB_TIMEOUT_SECONDS) -> bool:
    async def _probe() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
        return True
    except Exception as exc:  # includes asyncio.TimeoutError
        logger.warning("Readiness database check failed: exception_type=%s", type(exc).__name__)
        return False


async def readiness(engine) -> tuple[int, dict[str, Any]]:
    if not startup_state.complete:
        return 503, {"status": "starting", "checks": {"startup": "failed" if startup_state.failed_step else "pending"}}
    db_ok = await check_database(engine)
    if not db_ok:
        return 503, {"status": "unavailable", "checks": {"startup": "ok", "database": "unavailable"}}
    return 200, {"status": "ok", "checks": {"startup": "ok", "database": "ok"}}
