"""Background polling loop. Started only when TALITRADE_MT_API_WORKER_ENABLED is on.

Order of work in one round:
1. every connection whose live data is due, or that has a queued "refresh now" request
   (short: three broker calls each);
2. history for connections where it is due - bounded per pass by config.history_pass_seconds()
   - and after each one, any live work that fell due in the meantime.
So one account's import cannot hold up live balances, its own or anyone else's, for longer than
one bounded history pass.
"""
from __future__ import annotations

import asyncio
import logging
import random

from app.services.matchtrader_api import config, engine
from app.services.matchtrader_api.store import SqlStore

logger = logging.getLogger("app.matchtrader_api")

_wake: asyncio.Event | None = None


def nudge() -> None:
    """Wake the running worker early (a refresh was just queued). The queued row is the durable
    request; this only shortens the wait and is a no-op when no worker runs in this process."""
    if _wake is not None:
        _wake.set()


RETRY_AFTER_SKIP_SECONDS = 5


async def _pass(store: SqlStore, owner: str, connection_id: int, client_factory, *, live: bool, history: bool) -> None:
    """One scheduled pass. The pass itself sets the next due times in its last statement; this
    only steps in when it could not (lease busy, or it failed before reaching that statement),
    so a connection is never retried in a tight loop."""
    metrics: dict = {}
    try:
        await engine.sync_connection(store, connection_id, owner=owner, client_factory=client_factory, live=live, history=history,
                                     metrics=metrics, schedule=True)
    except Exception as exc:
        logger.error("matchtrader worker pass failed connection=%s live=%s history=%s error_type=%s", connection_id, live, history, type(exc).__name__)
    if not metrics.get("scheduled"):
        try:
            await store.schedule(connection_id, live_in=RETRY_AFTER_SKIP_SECONDS if live else None, history_in=RETRY_AFTER_SKIP_SECONDS if history else None)
        except Exception as exc:
            logger.error("matchtrader worker reschedule failed connection=%s error_type=%s", connection_id, type(exc).__name__)


async def _live_pass(store: SqlStore, owner: str, connection_id: int, client_factory) -> None:
    await _pass(store, owner, connection_id, client_factory, live=True, history=False)


async def _history_pass(store: SqlStore, owner: str, connection_id: int, client_factory) -> None:
    await _pass(store, owner, connection_id, client_factory, live=False, history=True)


async def run_due_once(store: SqlStore, *, client_factory=engine.default_client_factory) -> int:
    """Run one round for every connection that is due. One connection failing never blocks the others."""
    owner = engine.new_owner()
    due = await store.due_connections()
    handled = {d["id"] for d in due}
    for d in due:
        if d["live_due"] or d["sync_requested"]:
            await _live_pass(store, owner, d["id"], client_factory)
    for d in due:
        if not d["history_due"]:
            continue
        await _history_pass(store, owner, d["id"], client_factory)
        for other in await store.due_connections():   # live work that fell due while history ran
            if other["live_due"] or other["sync_requested"]:
                handled.add(other["id"])
                await _live_pass(store, owner, other["id"], client_factory)
    return len(handled)


IDLE_MAX_SECONDS, IDLE_MIN_SECONDS = 6.0, 0.25


async def _idle_seconds(store: SqlStore) -> float:
    """Sleep until the next scheduled work, within bounds, so a 30 s live interval is not
    stretched by a fixed poll period. Newly queued refreshes wake the loop through nudge()."""
    try:
        upcoming = await store.seconds_until_next_due()
    except Exception:
        upcoming = None
    if upcoming is None:
        return IDLE_MAX_SECONDS + random.random()
    return min(IDLE_MAX_SECONDS, max(IDLE_MIN_SECONDS, upcoming + 0.1)) + random.random() * 0.2


async def worker_loop(stop_event: asyncio.Event, store: SqlStore | None = None) -> None:
    global _wake
    store = store or SqlStore()
    _wake = asyncio.Event()
    logger.warning("Match-Trader API worker started (live every %ss, history every %ss)", config.live_interval_seconds(), config.history_interval_seconds())
    try:
        while not stop_event.is_set():
            _wake.clear()
            try:
                await run_due_once(store)
            except Exception as exc:
                logger.error("matchtrader worker loop error error_type=%s", type(exc).__name__)
            stop, wake = asyncio.ensure_future(stop_event.wait()), asyncio.ensure_future(_wake.wait())
            try:
                await asyncio.wait({stop, wake}, timeout=await _idle_seconds(store), return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
                wake.cancel()
    finally:
        _wake = None
