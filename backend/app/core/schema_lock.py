"""Serialize startup schema work across every instance that shares the database.

The web process creates and adjusts its tables at startup (CREATE TABLE IF NOT EXISTS, ALTER ... ADD,
one-time dedupe/backfill statements). When two instances start together (two Railway services on the same
database, or the old and new deployment overlapping during a deploy) those statements race: concurrent
CREATE TABLE IF NOT EXISTS can fail on the type catalog, and the dedupe DELETE can run while the other
instance is ingesting. A Postgres session-level advisory lock makes the whole startup schema phase run in
one instance at a time; the lock is released explicitly, and automatically if the holder's connection dies.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Arbitrary, fixed 64-bit key: "TaliTrade startup schema".
STARTUP_SCHEMA_LOCK_KEY = 7_412_026_001
DEFAULT_WAIT_SECONDS = 120.0
POLL_SECONDS = 0.5


class SchemaLockTimeout(RuntimeError):
    pass


@asynccontextmanager
async def startup_schema_lock(engine=None, wait_seconds: float = DEFAULT_WAIT_SECONDS, key: int = STARTUP_SCHEMA_LOCK_KEY):
    if engine is None:
        from app.core.database import engine as default_engine
        engine = default_engine
    if engine.dialect.name != "postgresql":
        yield
        return
    conn = await engine.connect()
    try:
        deadline = time.monotonic() + wait_seconds
        waited = False
        while True:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
            await conn.commit()
            if got:
                break
            if not waited:
                logger.info("Startup schema lock held by another instance; waiting")
                waited = True
            if time.monotonic() >= deadline:
                raise SchemaLockTimeout("Timed out waiting for the startup schema lock held by another instance")
            await asyncio.sleep(POLL_SECONDS)
        logger.info("Startup schema lock acquired")
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()
            except Exception as exc:  # the lock dies with the connection anyway
                logger.warning("Startup schema lock release failed: exception_type=%s", type(exc).__name__)
    finally:
        await conn.close()
