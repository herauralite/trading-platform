"""One-tap "import all available trade history" for a Match-Trader account.

How far back "all available" goes
---------------------------------
* Target: the broker-reported account creation time (``/v1/account`` ``createTime``) when it is
  plausible. Nothing can have closed before the account existed. If the broker does not report
  it, the target is ``FLOOR_MS`` (2000-01-01) and the status says the boundary was not reported.
* The work is ordinary backfill intervals, planned by ``SqlStore.request_older_history``: only the
  part older than what is already queued (the 7-day import queued at connect) is added, with a
  1 ms overlap, so a second tap or the connect-time import never starts an overlapping job.
* The documented contract has no per-request date limit and no cursor: a range is complete when
  the broker returns fewer than ``backfill.HISTORY_LIMIT`` rows; a full page is bisected. If a
  broker refuses a long range (4xx), the range is split by time (``backfill.WINDOW``), and that is
  reported as a window limit, never as "no trades".
* Done means: every backfill range from the target to the time of the request has been answered
  completely, with no recorded gaps. An empty answer for a range is only "no trades in that
  range"; the import never stops early because a range was empty.
* What cannot be known from the API: whether the broker deletes trades older than some retention
  period. The status therefore names the dates checked and the basis for the earliest date.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.services.matchtrader_api import config
from app.services.matchtrader_api.store import SqlStore, plausible_creation_ms

FLOOR_MS = 946_684_800_000                # 2000-01-01 UTC: no Trader API account is older
STALLED_SECONDS = 15 * 60                 # queued work with no broker answer for this long is reported as interrupted


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_iso(value) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


async def start(store: SqlStore, connection: dict, user_id: str, *, retry: bool = False) -> bool:
    """Queue the import for an owned, active connection. Idempotent: repeating it queues nothing
    new. Returns False if the connection is no longer this user's active connection."""
    created_ms = plausible_creation_ms(int(connection["account_created_at"].timestamp() * 1000)) if connection.get("account_created_at") else None
    from_ms, basis = (created_ms, "account_created_at") if created_ms else (FLOOR_MS, "not_reported")
    to_ms = int(time.time() * 1000)
    resources = [r for r in ("closed_positions", "ledgers") if (connection["capabilities"] or {}).get(r) is not False]
    planned = await store.request_older_history(connection["id"], user_id, from_ms, to_ms, resources)
    if planned is None:
        return False
    async with store.engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO mtapi_history_imports (connection_id, requested_by, target_from_ms, target_to_ms, basis)
            VALUES (:id, :user, :f, :t, :basis)
            ON CONFLICT (connection_id) DO UPDATE SET
                target_from_ms = LEAST(mtapi_history_imports.target_from_ms, EXCLUDED.target_from_ms),
                basis = CASE WHEN EXCLUDED.basis = 'account_created_at' THEN EXCLUDED.basis ELSE mtapi_history_imports.basis END,
                attempts = mtapi_history_imports.attempts + CASE WHEN CAST(:retry AS boolean) THEN 1 ELSE 0 END,
                last_retry_at = CASE WHEN CAST(:retry AS boolean) THEN NOW() ELSE mtapi_history_imports.last_retry_at END
        """), {"id": connection["id"], "user": user_id, "f": from_ms, "t": to_ms, "basis": basis, "retry": retry})
        if retry:   # start again now instead of waiting out a backoff
            await conn.execute(text("UPDATE mtapi_connections SET next_history_sync_at = NOW(), history_requested_at = NOW() WHERE id = :id AND status = 'active'"),
                               {"id": connection["id"]})
    return True


async def status(store: SqlStore, connection_id: int, user_id: str) -> dict | None:
    """What has actually been imported and checked for this user's connection, or None if it is
    not theirs. Counts come from committed rows only; nothing is estimated."""
    async with store.engine.connect() as conn:
        c = (await conn.execute(text("""SELECT id, status, capabilities, account_created_at FROM mtapi_connections
                                        WHERE id = :id AND user_id = :user AND status <> 'disconnected'"""),
                                {"id": connection_id, "user": user_id})).mappings().first()
        if not c:
            return None
        imp = (await conn.execute(text("SELECT * FROM mtapi_history_imports WHERE connection_id = :id"), {"id": connection_id})).mappings().first()
        iv = (await conn.execute(text("""
            SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                   COUNT(*) FILTER (WHERE status = 'gap') AS gaps,
                   COUNT(*) FILTER (WHERE status = 'split_window') AS window_splits,
                   MIN(range_from) AS requested_from,
                   MAX(range_to) AS requested_to,
                   MAX(range_to) FILTER (WHERE status = 'queued') AS newest_unchecked_to
            FROM mtapi_history_intervals WHERE connection_id = :id AND resource = 'closed_positions' AND kind = 'backfill'
        """), {"id": connection_id})).mappings().first()
        gaps = (await conn.execute(text("""SELECT range_from, range_to FROM mtapi_history_intervals
                 WHERE connection_id = :id AND resource = 'closed_positions' AND status = 'gap' ORDER BY range_from LIMIT 20"""), {"id": connection_id})).all()
        trades = (await conn.execute(text("""SELECT COUNT(*) AS n, MIN(close_time) AS first, MAX(close_time) AS last
                                             FROM mtapi_closed_positions WHERE connection_id = :id"""), {"id": connection_id})).mappings().first()
        st = (await conn.execute(text("""SELECT last_error_code, consecutive_failures, last_attempt_at, last_success_at, NOW() AS now
                                         FROM mtapi_sync_state WHERE connection_id = :id AND resource = 'closed_positions'"""), {"id": connection_id})).mappings().first()
        now = (await conn.execute(text("SELECT NOW()"))).scalar()
    permitted = (c["capabilities"] or {}).get("closed_positions") is not False
    queued, requested_from = int(iv["queued"] or 0), iv["requested_from"]
    # Ranges are worked newest first, so everything newer than the newest still-queued range has been answered.
    checked_from = (iv["newest_unchecked_to"] if queued else requested_from)
    checked_to = iv["requested_to"]
    if checked_from is not None and requested_from is not None:
        checked_from = max(checked_from - 1, requested_from + 1)       # undo the 1 ms widening on each side
    if checked_to is not None:
        checked_to -= 1
    retry_pending = bool(imp and imp["last_retry_at"] and (not st or not st["last_attempt_at"] or imp["last_retry_at"] > st["last_attempt_at"]))
    failing = bool(st and (st["consecutive_failures"] or 0) > 0) and not retry_pending   # a retry newer than the failure is in progress
    error = st["last_error_code"] if failing else None
    stalled = bool(queued and st and st["last_attempt_at"] and (now - st["last_attempt_at"]).total_seconds() > STALLED_SECONDS)
    if not permitted:
        state, reason = "unavailable", "not_permitted"
    elif imp is None:
        state, reason = "not_requested", None
    elif queued and not config.worker_enabled():
        state, reason = "interrupted", "worker_off"
    elif queued and failing and error != "rate_limited":
        state, reason = "interrupted", error
    elif queued and stalled:
        state, reason = "interrupted", "stalled"
    elif queued:
        state, reason = "importing", ("rate_limited" if error == "rate_limited" else None)
    elif int(iv["gaps"] or 0):
        state, reason = "limited", "saturated_millisecond"
    elif int(trades["n"] or 0) == 0:
        state, reason = "empty", None
    else:
        state, reason = "imported", None
    nothing_checked = checked_from is None or checked_to is None or checked_from >= checked_to
    return {
        "connection_id": connection_id, "state": state, "reason": reason,
        "closed_trades": int(trades["n"] or 0),                            # unique closed entries stored for this account (deduplicated by broker uid)
        "first_trade_at": _ts_iso(trades["first"]), "last_trade_at": _ts_iso(trades["last"]),
        "checked_from": None if nothing_checked else _iso(checked_from), "checked_to": None if nothing_checked else _iso(checked_to),
        "target_from": _iso(imp["target_from_ms"]) if imp else None,
        "earliest_boundary": (imp["basis"] if imp else None),               # account_created_at | not_reported
        "account_created_at": _ts_iso(c["account_created_at"]),
        "queued_ranges": queued, "window_limited": int(iv["window_splits"] or 0) > 0,
        "gaps": [[_iso(int(a) + 1), _iso(int(b) - 1)] for a, b in gaps],
        "attempts": imp["attempts"] if imp else 0,
        "requested_at": _ts_iso(imp["requested_at"]) if imp else None,
        "last_broker_answer_at": _ts_iso(st["last_success_at"]) if st else None,
        "retention_known": False,                                           # the API does not say how long the broker keeps closed trades
    }
