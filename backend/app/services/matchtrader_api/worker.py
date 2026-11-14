"""Background polling loop. Started only when TALITRADE_MT_API_WORKER_ENABLED is on."""
from __future__ import annotations

import asyncio
import logging
import random

from app.services.matchtrader_api import config, engine
from app.services.matchtrader_api.store import SqlStore

logger = logging.getLogger("app.matchtrader_api")


async def run_due_once(store: SqlStore, *, client_factory=engine.default_client_factory) -> int:
    """Run one pass for every connection that is due. One connection failing never blocks the others."""
    owner = engine.new_owner()
    handled = 0
    for due in await store.due_connections():
        connection_id = due["id"]
        try:
            await engine.sync_connection(store, connection_id, owner=owner, client_factory=client_factory,
                                         live=bool(due["live_due"]), history=bool(due["history_due"]))
            handled += 1
        except Exception as exc:
            logger.error("matchtrader worker pass failed connection=%s error_type=%s", connection_id, type(exc).__name__)
        finally:
            jitter = random.randint(0, 3)
            await store.schedule(connection_id,
                                 live_in=config.live_interval_seconds() + jitter if due["live_due"] else None,
                                 history_in=config.history_interval_seconds() + jitter if due["history_due"] else None)
    return handled


async def worker_loop(stop_event: asyncio.Event, store: SqlStore | None = None) -> None:
    store = store or SqlStore()
    logger.warning("Match-Trader API worker started (live every %ss, history every %ss)", config.live_interval_seconds(), config.history_interval_seconds())
    while not stop_event.is_set():
        try:
            await run_due_once(store)
        except Exception as exc:
            logger.error("matchtrader worker loop error error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5 + random.random() * 2)
        except asyncio.TimeoutError:
            pass
