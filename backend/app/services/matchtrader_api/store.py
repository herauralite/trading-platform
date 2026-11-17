"""PostgreSQL persistence for the Match-Trader API integration.

All tables are new and prefixed ``mtapi_``. Nothing here reads or writes the existing
``trades`` / ``positions`` / ``account_snapshots`` tables: imported data lives in its own
(shadow) namespace until a separately approved cutover.

Write safety
------------
Every write runs in one transaction that first locks the connection row and checks
* the connection is still ``active``,
* the caller's ``generation`` is current (it changes on rotate / disconnect / pause), and
* the caller still holds the sync lease.
A worker that lost any of these gets ``StaleWriter`` and its transaction rolls back, so an
old worker can never write after a rotation, disconnect or lease takeover.
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services.matchtrader_api import schemas
from app.services.matchtrader_api.backfill import COMPLETE, GAP, SPLIT, WINDOW

RESOURCES = ("summary", "positions", "pending_orders", "closed_positions", "ledgers")
HISTORY_RESOURCES = ("closed_positions", "ledgers")


class IdentityConflict(Exception):
    """The verified external account is already bound to a connection."""


class StaleWriter(Exception):
    """Generation, lease or status changed underneath this writer."""


def _default_engine():
    from app.core.database import engine

    return engine


def _dt(ms: int | None) -> datetime | None:
    return schemas.ms_to_datetime(ms)


EARLIEST_PLAUSIBLE_MS = 946_684_800_000          # 2000-01-01 UTC


def plausible_creation_ms(value) -> int | None:
    """The broker's account creation time, or None if it is missing or not believable."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return ms if EARLIEST_PLAUSIBLE_MS <= ms <= time.time() * 1000 + 86_400_000 else None


def _raw(model) -> str:
    return json.dumps(model.raw, default=str, sort_keys=True)


BULK_CHUNK = 500


def _unique(rows: list[dict], key: str) -> list[dict]:
    """Last occurrence wins, as with row-by-row upserts. (One INSERT .. ON CONFLICT statement may
    not touch the same key twice.)"""
    return list({row[key]: row for row in rows}.values())


def _chunks(rows: list[dict]):
    for start in range(0, len(rows), BULK_CHUNK):
        yield rows[start:start + BULK_CHUNK]


def _rows_json(rows: list[dict]) -> str:
    """Rows as one JSON parameter. Decimals travel as strings so no precision is lost on the way
    to NUMERIC; datetimes as ISO-8601."""
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)
    return json.dumps(rows, default=encode)


DDL = [
    """CREATE TABLE IF NOT EXISTS mtapi_connections (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        platform_host TEXT NOT NULL,
        external_login TEXT NOT NULL,
        broker_name TEXT,
        platform_url TEXT,
        account_currency TEXT,
        account_type TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        status_detail TEXT,
        generation INTEGER NOT NULL DEFAULT 1,
        ingestion_mode TEXT NOT NULL DEFAULT 'shadow',
        linked_external_account_id TEXT,
        capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
        token_hint TEXT,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        next_live_sync_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        next_history_sync_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        disconnected_at TIMESTAMPTZ
    )""",
    # One live connection per verified external identity. Disconnected rows are kept for history.
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_mtapi_connections_identity
       ON mtapi_connections (platform_host, external_login) WHERE status <> 'disconnected'""",
    "CREATE INDEX IF NOT EXISTS idx_mtapi_connections_user ON mtapi_connections (user_id)",
    # When the broker says the trading account was created (GET /v1/account createTime). A candidate
    # lower bound for "all available history"; it is not proof the broker keeps every trade since then.
    "ALTER TABLE mtapi_connections ADD COLUMN IF NOT EXISTS account_created_at TIMESTAMPTZ",
    # When the user last asked for (more) history: a pass that is finishing then keeps it due now.
    "ALTER TABLE mtapi_connections ADD COLUMN IF NOT EXISTS history_requested_at TIMESTAMPTZ",
    """CREATE TABLE IF NOT EXISTS mtapi_credentials (
        connection_id INTEGER PRIMARY KEY REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        ciphertext TEXT NOT NULL,
        key_version TEXT NOT NULL,
        generation INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_sync_state (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        resource TEXT NOT NULL,
        last_attempt_at TIMESTAMPTZ,
        last_success_at TIMESTAMPTZ,
        last_error_code TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        snapshot_at TIMESTAMPTZ,
        cursor_ms BIGINT,
        PRIMARY KEY (connection_id, resource)
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_account_summary (
        connection_id INTEGER PRIMARY KEY REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        balance NUMERIC, credit NUMERIC, equity NUMERIC, margin NUMERIC, free_funds NUMERIC,
        margin_level NUMERIC, profit NUMERIC, fetched_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_positions (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        external_id TEXT NOT NULL,
        symbol TEXT NOT NULL, alias TEXT, side TEXT, volume NUMERIC, open_price NUMERIC, current_price NUMERIC,
        commission NUMERIC, swap NUMERIC, profit NUMERIC, net_profit NUMERIC, margin NUMERIC,
        open_time TIMESTAMPTZ, tp_price NUMERIC, sl_price NUMERIC, trailing_distance NUMERIC, comment TEXT,
        fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, external_id)
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_pending_orders (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        external_id TEXT NOT NULL,
        symbol TEXT NOT NULL, alias TEXT, side TEXT, order_type TEXT, volume NUMERIC, activation_price NUMERIC,
        tp_price NUMERIC, sl_price NUMERIC, create_time TIMESTAMPTZ, last_update_time TIMESTAMPTZ, comment TEXT,
        fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, external_id)
    )""",
    # One row per closing entry (uid). Partial closes share position_id and have distinct uids.
    """CREATE TABLE IF NOT EXISTS mtapi_closed_positions (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        uid TEXT NOT NULL,
        position_id TEXT NOT NULL, closing_order_id TEXT,
        symbol TEXT NOT NULL, alias TEXT, side TEXT, volume NUMERIC, open_price NUMERIC, close_price NUMERIC,
        commission NUMERIC, swap NUMERIC, profit NUMERIC, net_profit NUMERIC,
        open_time TIMESTAMPTZ, close_time TIMESTAMPTZ NOT NULL, sl_price NUMERIC, tp_price NUMERIC,
        close_reason TEXT, comment TEXT,
        raw JSONB NOT NULL, previous_raw JSONB, revision INTEGER NOT NULL DEFAULT 1,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (connection_id, uid)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mtapi_closed_time ON mtapi_closed_positions (connection_id, close_time)",
    # Cash movements. Kept separate from trade performance: never summed with closed-position P&L.
    """CREATE TABLE IF NOT EXISTS mtapi_ledgers (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        external_id TEXT NOT NULL,
        entry_type TEXT, amount NUMERIC NOT NULL, entry_time TIMESTAMPTZ NOT NULL, comment TEXT,
        raw JSONB NOT NULL, previous_raw JSONB, revision INTEGER NOT NULL DEFAULT 1,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (connection_id, external_id)
    )""",
    # Durable work queue + coverage record for history. status: queued | done | gap
    """CREATE TABLE IF NOT EXISTS mtapi_history_intervals (
        id SERIAL PRIMARY KEY,
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        resource TEXT NOT NULL,
        range_from BIGINT NOT NULL, range_to BIGINT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'backfill',
        status TEXT NOT NULL DEFAULT 'queued',
        records INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ,
        UNIQUE (connection_id, resource, range_from, range_to)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mtapi_intervals_queue ON mtapi_history_intervals (connection_id, status, kind)",
    # One durable "refresh now" request per connection. state: queued | running | completed | partial | failed | cancelled.
    # The worker runs it under the normal lease; ``generation`` ties it to the credential it was asked for.
    # One "import all available trade history" request per connection (the one-tap import).
    # The work itself is ordinary backfill intervals; this row records what was asked for and why.
    # basis: account_created_at (the broker reported when the account was opened) | not_reported.
    """CREATE TABLE IF NOT EXISTS mtapi_history_imports (
        connection_id INTEGER PRIMARY KEY REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        requested_by TEXT NOT NULL,
        target_from_ms BIGINT NOT NULL,
        target_to_ms BIGINT NOT NULL,
        basis TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_retry_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_sync_requests (
        connection_id INTEGER PRIMARY KEY REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        request_id INTEGER NOT NULL DEFAULT 1,
        generation INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        detail TEXT,
        resources JSONB NOT NULL DEFAULT '{}'::jsonb,
        attempts INTEGER NOT NULL DEFAULT 0,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )""",
]

_CANCEL_REQUESTS = """UPDATE mtapi_sync_requests SET state = 'cancelled', detail = :why, finished_at = NOW()
                      WHERE connection_id = :id AND state IN ('queued', 'running')"""

DROP_ORDER = ["mtapi_history_imports", "mtapi_market_feeds", "mtapi_market_candles", "mtapi_market_quotes", "mtapi_market_symbols", "mtapi_sync_requests", "mtapi_history_intervals", "mtapi_ledgers", "mtapi_closed_positions", "mtapi_pending_orders",
              "mtapi_positions", "mtapi_account_summary", "mtapi_sync_state", "mtapi_credentials", "mtapi_connections"]


# ═══ One-statement operations ════════════════════════════════════════════════
# Each method below is ONE SQL statement (data-modifying CTEs), so it is atomic on its own and
# costs one database round trip. The fence is the ``g`` CTE: it locks the connection row and
# matches only while the caller's generation and lease are current; every write in the
# statement is conditional on it. On a slow database link the number of round trips, not the
# amount of data, decides how long a pass takes.
_GUARD = """g AS (SELECT id FROM mtapi_connections
                 WHERE id = :id AND status = 'active' AND generation = :gen AND lease_owner = :o AND lease_expires_at > NOW()
                 FOR UPDATE)"""

_STATE_UPSERT = """INSERT INTO mtapi_sync_state (connection_id, resource, last_attempt_at, last_success_at, last_error_code, consecutive_failures, snapshot_at)
        SELECT CAST(:id AS integer), x.resource, NOW(), CASE WHEN x.ok THEN NOW() END, x.err, CASE WHEN x.ok THEN 0 ELSE 1 END, x.snap
        FROM jsonb_to_recordset(CAST(:states AS jsonb)) AS x(resource text, ok boolean, err text, snap timestamptz)
        WHERE EXISTS (SELECT 1 FROM g)
        ON CONFLICT (connection_id, resource) DO UPDATE SET
            last_attempt_at = NOW(),
            last_success_at = CASE WHEN EXCLUDED.last_success_at IS NOT NULL THEN NOW() ELSE mtapi_sync_state.last_success_at END,
            last_error_code = EXCLUDED.last_error_code,
            consecutive_failures = CASE WHEN EXCLUDED.last_success_at IS NOT NULL THEN 0 ELSE mtapi_sync_state.consecutive_failures + 1 END,
            snapshot_at = COALESCE(EXCLUDED.snapshot_at, mtapi_sync_state.snapshot_at)
        RETURNING 1"""

# Renews the lease, or (final statement of a pass) releases it and sets the next due times.
_LEASE_UPDATE = """UPDATE mtapi_connections c SET
            lease_owner = CASE WHEN CAST(:release AS boolean) THEN NULL ELSE c.lease_owner END,
            lease_expires_at = CASE WHEN CAST(:release AS boolean) THEN NULL ELSE NOW() + make_interval(secs => CAST(:ttl AS double precision)) END,
            next_live_sync_at = CASE WHEN CAST(:live_in AS double precision) IS NULL THEN c.next_live_sync_at
                                     ELSE NOW() + make_interval(secs => CAST(:live_in AS double precision)) END,
            next_history_sync_at = CASE WHEN CAST(:history_in AS double precision) IS NULL THEN c.next_history_sync_at
                                        ELSE NOW() + make_interval(secs => CAST(:history_in AS double precision)) END
        FROM g WHERE c.id = g.id RETURNING 1"""

_FINISH_REQUEST = """UPDATE mtapi_sync_requests SET state = CAST(:req_state AS text), detail = CAST(:req_detail AS text),
            resources = CAST(:req_resources AS jsonb), finished_at = NOW()
        WHERE connection_id = :id AND request_id = CAST(:req_id AS integer) AND state = 'running' AND EXISTS (SELECT 1 FROM g)
        RETURNING 1"""

_POSITION_COLUMNS = """eid text, symbol text, alias text, side text, volume numeric, open_price numeric, current_price numeric,
                       commission numeric, swap numeric, profit numeric, net_profit numeric, margin numeric,
                       open_time timestamptz, tp numeric, sl numeric, trail numeric, comment text"""
_ORDER_COLUMNS = """eid text, symbol text, alias text, side text, type text, volume numeric, activation numeric, tp numeric, sl numeric,
                    created timestamptz, updated timestamptz, comment text"""

_SESSION: ContextVar = ContextVar("mtapi_pass_session", default=None)


class PassSession:
    """One database connection (autocommit) reused for a whole pass, plus timing counters."""

    def __init__(self, conn):
        self.conn = conn
        self.db_calls = 0
        self.db_seconds = 0.0


def _position_rows(positions: list) -> list[dict]:
    return _unique([{"eid": p.id, "symbol": p.symbol, "alias": p.alias, "side": p.side, "volume": p.volume, "open_price": p.openPrice,
                     "current_price": p.currentPrice, "commission": p.commission, "swap": p.swap, "profit": p.profit, "net_profit": p.netProfit,
                     "margin": p.margin, "open_time": _dt(p.openTime), "tp": p.tpPrice, "sl": p.slPrice, "trail": p.trailingDistance,
                     "comment": p.comment} for p in positions], "eid")


def _order_rows(orders: list) -> list[dict]:
    return _unique([{"eid": o.id, "symbol": o.symbol, "alias": o.alias, "side": o.side, "type": o.type, "volume": o.volume,
                     "activation": o.activationPrice, "tp": o.tpPrice, "sl": o.slPrice, "created": _dt(o.createTime),
                     "updated": _dt(o.lastUpdateTime), "comment": o.comment} for o in orders], "eid")


class FastOps:
    """Mixed into SqlStore."""

    @asynccontextmanager
    async def session(self):
        """Hold one connection for a pass. Statements issued through ``_one`` reuse it; they must
        be awaited one at a time (a connection runs one statement at a time)."""
        current = _SESSION.get()
        if current is not None:
            yield current
            return
        async with self.engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            session = PassSession(conn)
            token = _SESSION.set(session)
            try:
                yield session
            finally:
                _SESSION.reset(token)

    async def _one(self, sql: str, params: dict) -> list[dict]:
        """Run one self-contained statement and return its rows."""
        session = _SESSION.get()
        if session is None:
            # No pass in progress (a web request): still one round trip - autocommit, no BEGIN/COMMIT.
            async with self.engine.connect() as raw:
                conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
                return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        started = time.monotonic()
        try:
            return [dict(r) for r in (await session.conn.execute(text(sql), params)).mappings().all()]
        finally:
            session.db_calls += 1
            session.db_seconds += time.monotonic() - started

    async def open_pass(self, connection_id: int, owner: str, ttl_seconds: int, *, claim_request: bool) -> dict | None:
        """Take the lease and return everything a pass needs: the connection row, the stored
        credential, when live data is next due, and (optionally) the claimed refresh request. None if the
        connection is missing, not active, or leased by someone else."""
        rows = await self._one("""
            WITH c AS (
                UPDATE mtapi_connections
                SET lease_owner = :o, lease_expires_at = NOW() + make_interval(secs => CAST(:ttl AS double precision))
                WHERE id = :id AND status = 'active' AND (lease_owner IS NULL OR lease_owner = :o OR lease_expires_at < NOW())
                RETURNING *),
            r AS (
                UPDATE mtapi_sync_requests q
                SET state = 'running', started_at = COALESCE(q.started_at, NOW()), attempts = q.attempts + 1
                FROM c WHERE q.connection_id = c.id AND CAST(:claim AS boolean) AND q.state IN ('queued', 'running')
                RETURNING q.request_id, q.attempts)
            SELECT c.*, cr.ciphertext, cr.generation AS credential_generation,
                   (SELECT request_id FROM r) AS request_id, (SELECT attempts FROM r) AS request_attempts,
                   EXTRACT(EPOCH FROM c.next_live_sync_at - NOW()) AS live_due_in     -- on the database clock: no app/database clock skew
            FROM c LEFT JOIN mtapi_credentials cr ON cr.connection_id = c.id
        """, {"id": connection_id, "o": owner, "ttl": ttl_seconds, "claim": claim_request})
        return rows[0] if rows else None

    async def save_live(self, connection_id: int, generation: int, owner: str, ttl_seconds: int, *, summary=None, positions: list | None = None,
                        pending: list | None = None, results: dict[str, str], fetched_at: dict[str, datetime],
                        finish_request: dict | None = None, release: bool = False, live_in: float | None = None, history_in: float | None = None) -> None:
        """Store one live refresh in a single statement: summary, the full position set, the full
        pending-order set, per-resource sync state, the lease, and (if given) the refresh request.

        ``positions`` / ``pending`` are None when that fetch failed: the stored set is then left
        exactly as it was. ``results`` maps each attempted resource to "ok" or an error code."""
        summary_rows = [] if summary is None else [{"balance": summary.balance, "credit": summary.credit, "equity": summary.equity, "margin": summary.margin,
                                                    "free_funds": summary.freeFunds, "margin_level": summary.marginLevel, "profit": summary.profit}]
        states = [{"resource": name, "ok": value == "ok", "err": None if value == "ok" else value,
                   "snap": fetched_at.get(name) if value == "ok" else None} for name, value in results.items()]
        request = finish_request or {}
        rows = await self._one(f"""
            WITH {_GUARD},
            sm AS (
                INSERT INTO mtapi_account_summary (connection_id, balance, credit, equity, margin, free_funds, margin_level, profit, fetched_at)
                SELECT CAST(:id AS integer), x.balance, x.credit, x.equity, x.margin, x.free_funds, x.margin_level, x.profit, CAST(:summary_at AS timestamptz)
                FROM jsonb_to_recordset(CAST(:summary AS jsonb)) AS x(balance numeric, credit numeric, equity numeric, margin numeric,
                                                                      free_funds numeric, margin_level numeric, profit numeric)
                WHERE EXISTS (SELECT 1 FROM g)
                ON CONFLICT (connection_id) DO UPDATE SET balance = EXCLUDED.balance, credit = EXCLUDED.credit, equity = EXCLUDED.equity,
                    margin = EXCLUDED.margin, free_funds = EXCLUDED.free_funds, margin_level = EXCLUDED.margin_level,
                    profit = EXCLUDED.profit, fetched_at = EXCLUDED.fetched_at
                WHERE mtapi_account_summary.fetched_at < EXCLUDED.fetched_at
                RETURNING 1),
            -- A set is replaced only from a successful full response that is newer than the stored one.
            pos_ok AS (SELECT 1 FROM g WHERE CAST(:has_positions AS boolean) AND NOT EXISTS (
                SELECT 1 FROM mtapi_sync_state WHERE connection_id = :id AND resource = 'positions' AND snapshot_at >= CAST(:positions_at AS timestamptz))),
            pos_del AS (
                DELETE FROM mtapi_positions p WHERE p.connection_id = :id AND EXISTS (SELECT 1 FROM pos_ok)
                  AND p.external_id NOT IN (SELECT x.eid FROM jsonb_to_recordset(CAST(:positions AS jsonb)) AS x(eid text))
                RETURNING 1),
            pos_put AS (
                INSERT INTO mtapi_positions (connection_id, external_id, symbol, alias, side, volume, open_price, current_price, commission, swap,
                    profit, net_profit, margin, open_time, tp_price, sl_price, trailing_distance, comment, fetched_at)
                SELECT CAST(:id AS integer), x.eid, x.symbol, x.alias, x.side, x.volume, x.open_price, x.current_price, x.commission, x.swap,
                    x.profit, x.net_profit, x.margin, x.open_time, x.tp, x.sl, x.trail, x.comment, CAST(:positions_at AS timestamptz)
                FROM jsonb_to_recordset(CAST(:positions AS jsonb)) AS x({_POSITION_COLUMNS})
                WHERE EXISTS (SELECT 1 FROM pos_ok)
                ON CONFLICT (connection_id, external_id) DO UPDATE SET symbol = EXCLUDED.symbol, alias = EXCLUDED.alias, side = EXCLUDED.side,
                    volume = EXCLUDED.volume, open_price = EXCLUDED.open_price, current_price = EXCLUDED.current_price,
                    commission = EXCLUDED.commission, swap = EXCLUDED.swap, profit = EXCLUDED.profit, net_profit = EXCLUDED.net_profit,
                    margin = EXCLUDED.margin, open_time = EXCLUDED.open_time, tp_price = EXCLUDED.tp_price, sl_price = EXCLUDED.sl_price,
                    trailing_distance = EXCLUDED.trailing_distance, comment = EXCLUDED.comment, fetched_at = EXCLUDED.fetched_at
                RETURNING 1),
            ord_ok AS (SELECT 1 FROM g WHERE CAST(:has_pending AS boolean) AND NOT EXISTS (
                SELECT 1 FROM mtapi_sync_state WHERE connection_id = :id AND resource = 'pending_orders' AND snapshot_at >= CAST(:pending_at AS timestamptz))),
            ord_del AS (
                DELETE FROM mtapi_pending_orders p WHERE p.connection_id = :id AND EXISTS (SELECT 1 FROM ord_ok)
                  AND p.external_id NOT IN (SELECT x.eid FROM jsonb_to_recordset(CAST(:pending AS jsonb)) AS x(eid text))
                RETURNING 1),
            ord_put AS (
                INSERT INTO mtapi_pending_orders (connection_id, external_id, symbol, alias, side, order_type, volume, activation_price,
                    tp_price, sl_price, create_time, last_update_time, comment, fetched_at)
                SELECT CAST(:id AS integer), x.eid, x.symbol, x.alias, x.side, x.type, x.volume, x.activation, x.tp, x.sl, x.created, x.updated,
                    x.comment, CAST(:pending_at AS timestamptz)
                FROM jsonb_to_recordset(CAST(:pending AS jsonb)) AS x({_ORDER_COLUMNS})
                WHERE EXISTS (SELECT 1 FROM ord_ok)
                ON CONFLICT (connection_id, external_id) DO UPDATE SET symbol = EXCLUDED.symbol, alias = EXCLUDED.alias, side = EXCLUDED.side,
                    order_type = EXCLUDED.order_type, volume = EXCLUDED.volume, activation_price = EXCLUDED.activation_price,
                    tp_price = EXCLUDED.tp_price, sl_price = EXCLUDED.sl_price, create_time = EXCLUDED.create_time,
                    last_update_time = EXCLUDED.last_update_time, comment = EXCLUDED.comment, fetched_at = EXCLUDED.fetched_at
                RETURNING 1),
            st AS ({_STATE_UPSERT}),
            fin AS ({_FINISH_REQUEST}),
            lease AS ({_LEASE_UPDATE})
            SELECT EXISTS (SELECT 1 FROM g) AS ok
        """, {"id": connection_id, "gen": generation, "o": owner, "ttl": ttl_seconds,
              "summary": _rows_json(summary_rows), "summary_at": fetched_at.get("summary"),
              "has_positions": positions is not None, "positions": _rows_json(_position_rows(positions or [])), "positions_at": fetched_at.get("positions"),
              "has_pending": pending is not None, "pending": _rows_json(_order_rows(pending or [])), "pending_at": fetched_at.get("pending_orders"),
              "states": _rows_json(states),
              "req_id": request.get("request_id"), "req_state": request.get("state"), "req_detail": request.get("detail"),
              "req_resources": json.dumps(request.get("resources") or {}),
              "release": release, "live_in": live_in, "history_in": history_in})
        if not rows or not rows[0]["ok"]:
            raise StaleWriter()

    async def close_pass(self, connection_id: int, owner: str, *, live_in: float | None = None, history_in: float | None = None,
                         finish_request: dict | None = None, pass_seconds: float | None = None) -> None:
        """Last statement of a pass: release the lease (only if still ours), set the next due
        times, and record the refresh request's outcome if one is still open."""
        request = finish_request or {}
        await self._one("""
            WITH rel AS (
                UPDATE mtapi_connections c SET lease_owner = NULL, lease_expires_at = NULL,
                    next_live_sync_at = CASE WHEN CAST(:live_in AS double precision) IS NULL THEN c.next_live_sync_at
                                             ELSE NOW() + make_interval(secs => CAST(:live_in AS double precision)) END,
                    next_history_sync_at = CASE WHEN CAST(:history_in AS double precision) IS NULL THEN c.next_history_sync_at
                                                -- history the user asked for while this pass ran starts now, not an interval later
                                                WHEN CAST(:pass_seconds AS double precision) IS NOT NULL
                                                     AND c.history_requested_at >= NOW() - make_interval(secs => CAST(:pass_seconds AS double precision)) THEN NOW()
                                                ELSE NOW() + make_interval(secs => CAST(:history_in AS double precision)) END
                WHERE c.id = :id AND c.lease_owner = :o RETURNING 1),
            fin AS (
                UPDATE mtapi_sync_requests SET state = CAST(:req_state AS text), detail = CAST(:req_detail AS text),
                    resources = CAST(:req_resources AS jsonb), finished_at = NOW()
                WHERE connection_id = :id AND request_id = CAST(:req_id AS integer) AND state = 'running'
                  AND EXISTS (SELECT 1 FROM rel)       -- only the lease holder reports; otherwise the next holder re-runs it
                RETURNING 1)
            SELECT (SELECT COUNT(*) FROM rel) AS released, (SELECT COUNT(*) FROM fin) AS finished
        """, {"id": connection_id, "o": owner, "live_in": live_in, "history_in": history_in, "pass_seconds": pass_seconds,
              "req_id": request.get("request_id"), "req_state": request.get("state"), "req_detail": request.get("detail"),
              "req_resources": json.dumps(request.get("resources") or {})})

    async def plan_history(self, connection_id: int, generation: int, owner: str, windows: list[dict]) -> None:
        """Queue this pass's recent window for each resource and move its cursor, atomically.

        ``windows``: {resource, first_start, cutoff, overlap, reconcile_from, reconcile_every}.
        * If the previous recent window is still queued (nothing has fetched it yet) it is
          EXTENDED to the new cutoff instead of adding another one. The extended range is a
          superset of both, so nothing is skipped, and slow passes cannot pile up recent windows.
        * The window is durable in the same statement that moves the cursor: a failed fetch is
          retried, never skipped.
        * A wider reconcile window is queued when the last one is older than ``reconcile_every``."""
        rows = await self._one(f"""
            WITH {_GUARD},
            w AS (
                SELECT x.resource, s.cursor_ms AS cursor_ms, COALESCE(s.cursor_ms - x.overlap, x.first_start) - 1 AS range_from, x.cutoff + 1 AS range_to,
                       x.reconcile_from, x.reconcile_every
                FROM g, jsonb_to_recordset(CAST(:windows AS jsonb)) AS x(resource text, first_start bigint, cutoff bigint, overlap bigint,
                                                                         reconcile_from bigint, reconcile_every double precision)
                LEFT JOIN mtapi_sync_state s ON s.connection_id = :id AND s.resource = x.resource),
            ext AS (
                UPDATE mtapi_history_intervals h SET range_to = w.range_to
                FROM w WHERE h.connection_id = :id AND h.resource = w.resource AND h.kind = 'incremental' AND h.status = 'queued'
                  AND h.range_to = w.cursor_ms AND h.range_to < w.range_to
                RETURNING h.resource),
            ins AS (
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                SELECT CAST(:id AS integer), w.resource, w.range_from, w.range_to, 'incremental' FROM w
                WHERE w.resource NOT IN (SELECT resource FROM ext)
                ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING RETURNING 1),
            cur AS (
                INSERT INTO mtapi_sync_state (connection_id, resource, cursor_ms)
                SELECT CAST(:id AS integer), w.resource, w.range_to FROM w
                ON CONFLICT (connection_id, resource) DO UPDATE SET cursor_ms = GREATEST(COALESCE(mtapi_sync_state.cursor_ms, 0), EXCLUDED.cursor_ms)
                RETURNING 1),
            rec AS (
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                SELECT CAST(:id AS integer), w.resource, w.reconcile_from, w.range_to, 'reconcile' FROM w
                WHERE w.reconcile_from IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM mtapi_history_intervals h2
                    WHERE h2.connection_id = :id AND h2.resource = w.resource AND h2.kind = 'reconcile'
                      AND h2.created_at > NOW() - make_interval(secs => w.reconcile_every))
                ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING RETURNING 1)
            SELECT EXISTS (SELECT 1 FROM g) AS ok
        """, {"id": connection_id, "gen": generation, "o": owner, "windows": json.dumps(windows)})
        if not rows or not rows[0]["ok"]:
            raise StaleWriter()

    async def history_candidates(self, connection_id: int, per_class: int) -> list[dict]:
        """The next queued ranges of every (resource, kind), newest first within each."""
        return await self._one("""
            SELECT * FROM (
                SELECT h.*, row_number() OVER (PARTITION BY h.resource, h.kind ORDER BY h.range_to DESC, h.id) AS position
                FROM mtapi_history_intervals h WHERE h.connection_id = :id AND h.status = 'queued') q
            WHERE q.position <= :n ORDER BY q.resource, q.kind, q.position
        """, {"id": connection_id, "n": per_class})

    async def request_sync_owned(self, connection_id: int, user_id: str) -> dict | None:
        """Queue a refresh for a connection this user owns, in one statement.

        Returns None when the connection is not theirs (or does not exist); otherwise
        {status, created, request...}. Nothing is queued unless the connection is active, and an
        already pending request is returned as it is."""
        rows = await self._one("""
            WITH c AS (
                SELECT id, status, generation FROM mtapi_connections
                WHERE id = :id AND user_id = :user AND status <> 'disconnected' FOR UPDATE),
            ex AS (SELECT q.* FROM mtapi_sync_requests q JOIN c ON c.id = q.connection_id),
            ins AS (
                INSERT INTO mtapi_sync_requests (connection_id, request_id, generation, state)
                SELECT c.id, 1, c.generation, 'queued' FROM c
                WHERE c.status = 'active' AND NOT EXISTS (SELECT 1 FROM ex WHERE ex.state IN ('queued', 'running'))
                ON CONFLICT (connection_id) DO UPDATE SET request_id = mtapi_sync_requests.request_id + 1, generation = EXCLUDED.generation,
                    state = 'queued', detail = NULL, resources = '{}'::jsonb, attempts = 0, requested_at = NOW(), started_at = NULL, finished_at = NULL
                RETURNING *)
            SELECT c.status AS connection_status, (i.connection_id IS NOT NULL) AS created,
                   COALESCE(i.request_id, e.request_id) AS request_id, COALESCE(i.state, e.state) AS state,
                   CASE WHEN i.connection_id IS NOT NULL THEN i.detail ELSE e.detail END AS detail,
                   COALESCE(i.resources, e.resources) AS resources, COALESCE(i.requested_at, e.requested_at) AS requested_at,
                   CASE WHEN i.connection_id IS NOT NULL THEN i.started_at ELSE e.started_at END AS started_at,
                   CASE WHEN i.connection_id IS NOT NULL THEN i.finished_at ELSE e.finished_at END AS finished_at
            FROM c LEFT JOIN ins i ON TRUE LEFT JOIN ex e ON TRUE
        """, {"id": connection_id, "user": user_id})
        return rows[0] if rows else None


class SqlStore(FastOps):
    def __init__(self, engine_getter: Callable[[], Any] = _default_engine):
        self._engine_getter = engine_getter

    @property
    def engine(self):
        return self._engine_getter()

    async def ensure_schema(self) -> None:
        async with self.engine.begin() as conn:
            for statement in DDL:
                await conn.execute(text(statement))
        from app.services.matchtrader_api import config, market   # local import: market builds on this module
        if config.market_data_enabled():                           # market tables exist only when that feature is on
            await market.ensure_schema(self)

    # ── connections ──────────────────────────────────────────────────────────
    async def create_connection(self, *, user_id: str, identity: dict, capabilities: dict, status: str, status_detail: str | None,
                                linked_external_account_id: str | None, token_hint: str, encrypt: Callable[[int], str], key_version: Callable[[str], str],
                                queue_first_sync: bool = False, backfill_range: tuple[int, int] | None = None) -> dict:
        """Store the connection and its encrypted credential in one transaction. For an active
        connection the same transaction can also queue the first refresh and the initial history
        backfill, so the browser has nothing further to set up after the account is bound."""
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(text("""
                    INSERT INTO mtapi_connections (user_id, platform_host, external_login, broker_name, platform_url,
                        account_currency, account_type, status, status_detail, capabilities, linked_external_account_id, token_hint, account_created_at)
                    VALUES (:user_id, :platform_host, :external_login, :broker_name, :platform_url,
                        :account_currency, :account_type, :status, :status_detail, CAST(:capabilities AS jsonb), :linked, :token_hint,
                        CASE WHEN CAST(:account_created_ms AS bigint) IS NULL THEN NULL ELSE to_timestamp(CAST(:account_created_ms AS bigint) / 1000.0) END)
                    RETURNING *
                """), {**identity, "account_created_ms": plausible_creation_ms(identity.get("account_created_ms")), "user_id": user_id, "status": status, "status_detail": status_detail,
                       "capabilities": json.dumps(capabilities), "linked": linked_external_account_id, "token_hint": token_hint})).mappings().first()
                blob = encrypt(row["id"])
                active = row["status"] == "active"
                backfill = [{"resource": r, "f": backfill_range[0], "t": backfill_range[1]} for r in HISTORY_RESOURCES
                            if (capabilities or {}).get(r) is not False] if (active and backfill_range) else []
                extra = (await conn.execute(text("""
                    WITH cred AS (
                        INSERT INTO mtapi_credentials (connection_id, ciphertext, key_version, generation)
                        VALUES (:id, :c, :k, :g) RETURNING 1),
                    req AS (
                        INSERT INTO mtapi_sync_requests (connection_id, request_id, generation, state)
                        SELECT CAST(:id AS integer), 1, CAST(:g AS integer), 'queued' WHERE CAST(:queue AS boolean)
                        RETURNING request_id, state, detail, resources, requested_at, started_at, finished_at),
                    bf AS (
                        INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                        SELECT CAST(:id AS integer), x.resource, x.f, x.t, 'backfill'
                        FROM jsonb_to_recordset(CAST(:backfill AS jsonb)) AS x(resource text, f bigint, t bigint)
                        ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING RETURNING resource)
                    SELECT (SELECT COUNT(*) FROM cred) AS stored, (SELECT COUNT(*) FROM bf) AS backfills, req.*
                    FROM (SELECT 1) one LEFT JOIN req ON TRUE
                """), {"id": row["id"], "c": blob, "k": key_version(blob), "g": row["generation"], "queue": bool(active and queue_first_sync),
                       "backfill": json.dumps(backfill)})).mappings().first()
                created = dict(row)
                created["sync_request"] = dict(extra) if extra and extra["request_id"] is not None else None
                created["initial_backfill_resources"] = [b["resource"] for b in backfill]
                return created
        except IntegrityError:
            raise IdentityConflict() from None

    async def get_connection(self, connection_id: int) -> dict | None:
        rows = await self._one("SELECT * FROM mtapi_connections WHERE id = :id", {"id": connection_id})
        return rows[0] if rows else None

    async def list_connections(self, user_id: str) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM mtapi_connections WHERE user_id = :u AND status <> 'disconnected' ORDER BY id"), {"u": user_id})).mappings().all()
        return [dict(r) for r in rows]

    async def get_credential(self, connection_id: int) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT ciphertext, key_version, generation FROM mtapi_credentials WHERE connection_id = :id"), {"id": connection_id})).mappings().first()
        return dict(row) if row else None

    async def set_status(self, connection_id: int, status: str, detail: str | None = None, *, bump_generation: bool = True,
                         only_from: tuple[str, ...] | None = None, cancel_requests: bool = True) -> bool:
        """Change status. Bumping the generation fences every in-flight writer. A pending refresh
        request is cancelled too, unless the caller is the sync pass that will report its own result."""
        clause = "AND status = ANY(CAST(:only_from AS text[]))" if only_from else "AND status <> 'disconnected'"
        async with self.engine.begin() as conn:
            result = await conn.execute(text(f"""
                UPDATE mtapi_connections
                SET status = :s, status_detail = :d, updated_at = NOW(),
                    generation = generation + :bump, lease_owner = NULL, lease_expires_at = NULL,
                    next_live_sync_at = NOW(), next_history_sync_at = NOW()
                WHERE id = :id {clause}
            """), {"id": connection_id, "s": status, "d": detail, "bump": 1 if bump_generation else 0, "only_from": list(only_from or ())})
            if result.rowcount and bump_generation:
                await conn.execute(text("UPDATE mtapi_credentials SET generation = (SELECT generation FROM mtapi_connections WHERE id = :id) WHERE connection_id = :id"), {"id": connection_id})
            if result.rowcount and cancel_requests:
                await conn.execute(text(_CANCEL_REQUESTS), {"id": connection_id, "why": "connection_changed"})
        return bool(result.rowcount)

    async def rotate_credential(self, connection_id: int, *, encrypt: Callable[[int], str], key_version: Callable[[str], str], token_hint: str, capabilities: dict,
                                account_created_ms: int | None = None) -> dict:
        async with self.engine.begin() as conn:
            row = (await conn.execute(text("""
                UPDATE mtapi_connections
                SET generation = generation + 1, status = 'active', status_detail = NULL, token_hint = :hint,
                    account_created_at = COALESCE(CASE WHEN CAST(:created AS bigint) IS NULL THEN NULL ELSE to_timestamp(CAST(:created AS bigint) / 1000.0) END,
                                                  account_created_at),
                    capabilities = CAST(:caps AS jsonb), lease_owner = NULL, lease_expires_at = NULL,
                    next_live_sync_at = NOW(), next_history_sync_at = NOW(), updated_at = NOW()
                WHERE id = :id AND status <> 'disconnected' RETURNING *
            """), {"id": connection_id, "hint": token_hint, "caps": json.dumps(capabilities), "created": plausible_creation_ms(account_created_ms)})).mappings().first()
            if not row:
                raise StaleWriter()
            blob = encrypt(connection_id)
            await conn.execute(text("""
                INSERT INTO mtapi_credentials (connection_id, ciphertext, key_version, generation) VALUES (:id, :c, :k, :g)
                ON CONFLICT (connection_id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, key_version = EXCLUDED.key_version,
                    generation = EXCLUDED.generation, created_at = NOW()
            """), {"id": connection_id, "c": blob, "k": key_version(blob), "g": row["generation"]})
            await conn.execute(text(_CANCEL_REQUESTS), {"id": connection_id, "why": "token_replaced"})
            return dict(row)

    async def disconnect(self, connection_id: int) -> bool:
        """Stop syncing and delete the stored credential. Imported history is retained."""
        async with self.engine.begin() as conn:
            result = await conn.execute(text("""
                UPDATE mtapi_connections SET status = 'disconnected', status_detail = NULL, generation = generation + 1,
                    lease_owner = NULL, lease_expires_at = NULL, token_hint = NULL, disconnected_at = NOW(), updated_at = NOW()
                WHERE id = :id AND status <> 'disconnected'
            """), {"id": connection_id})
            await conn.execute(text("DELETE FROM mtapi_credentials WHERE connection_id = :id"), {"id": connection_id})
            await conn.execute(text("UPDATE mtapi_history_intervals SET status = 'cancelled' WHERE connection_id = :id AND status = 'queued'"), {"id": connection_id})
            await conn.execute(text(_CANCEL_REQUESTS), {"id": connection_id, "why": "disconnected"})
        return bool(result.rowcount)

    # ── leases ───────────────────────────────────────────────────────────────
    async def acquire_lease(self, connection_id: int, owner: str, ttl_seconds: int) -> int | None:
        """Take (or renew) the single-writer lease. Returns the current generation, or None if busy/inactive."""
        async with self.engine.begin() as conn:
            row = (await conn.execute(text("""
                UPDATE mtapi_connections
                SET lease_owner = :o, lease_expires_at = NOW() + make_interval(secs => :ttl)
                WHERE id = :id AND status = 'active'
                  AND (lease_owner IS NULL OR lease_owner = :o OR lease_expires_at < NOW())
                RETURNING generation
            """), {"id": connection_id, "o": owner, "ttl": ttl_seconds})).first()
        return int(row[0]) if row else None

    async def release_lease(self, connection_id: int, owner: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET lease_owner = NULL, lease_expires_at = NULL WHERE id = :id AND lease_owner = :o"),
                               {"id": connection_id, "o": owner})

    async def _guard(self, conn, connection_id: int, generation: int, owner: str) -> None:
        row = (await conn.execute(text("SELECT status, generation, lease_owner, lease_expires_at > NOW() AS lease_live FROM mtapi_connections WHERE id = :id FOR UPDATE"),
                                  {"id": connection_id})).mappings().first()
        if not row or row["status"] != "active" or row["generation"] != generation or row["lease_owner"] != owner or not row["lease_live"]:
            raise StaleWriter()

    # ── sync state ───────────────────────────────────────────────────────────
    async def _touch_state(self, conn, connection_id: int, resource: str, *, ok: bool, error: str | None = None,
                           snapshot_at: datetime | None = None, cursor_ms: int | None = None) -> None:
        await conn.execute(text("""
            INSERT INTO mtapi_sync_state (connection_id, resource, last_attempt_at, last_success_at, last_error_code, consecutive_failures, snapshot_at, cursor_ms)
            VALUES (:id, :r, NOW(), CASE WHEN :ok THEN NOW() END, :err, CASE WHEN :ok THEN 0 ELSE 1 END, :snap, :cur)
            ON CONFLICT (connection_id, resource) DO UPDATE SET
                last_attempt_at = NOW(),
                last_success_at = CASE WHEN :ok THEN NOW() ELSE mtapi_sync_state.last_success_at END,
                last_error_code = :err,
                consecutive_failures = CASE WHEN :ok THEN 0 ELSE mtapi_sync_state.consecutive_failures + 1 END,
                snapshot_at = COALESCE(:snap, mtapi_sync_state.snapshot_at),
                cursor_ms = COALESCE(:cur, mtapi_sync_state.cursor_ms)
        """), {"id": connection_id, "r": resource, "ok": ok, "err": error, "snap": snapshot_at, "cur": cursor_ms})

    async def record_failure(self, connection_id: int, resource: str, error: str) -> None:
        async with self.engine.begin() as conn:
            await self._touch_state(conn, connection_id, resource, ok=False, error=error)

    async def get_sync_state(self, connection_id: int) -> dict[str, dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM mtapi_sync_state WHERE connection_id = :id"), {"id": connection_id})).mappings().all()
        return {r["resource"]: dict(r) for r in rows}

    async def schedule(self, connection_id: int, *, live_in: int | None = None, history_in: int | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("""
                UPDATE mtapi_connections SET
                    next_live_sync_at = CASE WHEN CAST(:live AS integer) IS NULL THEN next_live_sync_at ELSE NOW() + make_interval(secs => CAST(:live AS integer)) END,
                    next_history_sync_at = CASE WHEN CAST(:hist AS integer) IS NULL THEN next_history_sync_at ELSE NOW() + make_interval(secs => CAST(:hist AS integer)) END
                WHERE id = :id
            """), {"id": connection_id, "live": live_in, "hist": history_in})

    async def due_connections(self, limit: int = 20) -> list[dict]:
        """Connections with work to do and no live lease. Requested refreshes come first."""
        return await self._one("""
            SELECT c.id, c.next_live_sync_at <= NOW() AS live_due, c.next_history_sync_at <= NOW() AS history_due,
                   COALESCE(r.state IN ('queued', 'running'), FALSE) AS sync_requested
            FROM mtapi_connections c LEFT JOIN mtapi_sync_requests r ON r.connection_id = c.id
            WHERE c.status = 'active'
              AND (c.next_live_sync_at <= NOW() OR c.next_history_sync_at <= NOW() OR COALESCE(r.state IN ('queued', 'running'), FALSE))
              AND (c.lease_owner IS NULL OR c.lease_expires_at < NOW())
            ORDER BY COALESCE(r.state IN ('queued', 'running'), FALSE) DESC, LEAST(c.next_live_sync_at, c.next_history_sync_at) LIMIT :n
        """, {"n": limit})

    async def seconds_until_next_due(self) -> float | None:
        """Seconds until the earliest scheduled work on any active connection (None when there is none)."""
        rows = await self._one("""
            SELECT EXTRACT(EPOCH FROM MIN(LEAST(next_live_sync_at, next_history_sync_at)) - NOW()) AS seconds
            FROM mtapi_connections WHERE status = 'active'
        """, {})
        value = rows[0]["seconds"] if rows else None
        return float(value) if value is not None else None

    # ── refresh requests ─────────────────────────────────────────────────────
    async def request_sync(self, connection_id: int) -> tuple[dict | None, bool]:
        """Queue one refresh for an active connection. Returns (request, created).

        Account-scoped and idempotent: while a request is queued or running, asking again returns
        that same request (created=False) instead of adding a second job."""
        async with self.engine.begin() as conn:
            row = (await conn.execute(text("SELECT status, generation FROM mtapi_connections WHERE id = :id FOR UPDATE"), {"id": connection_id})).mappings().first()
            if not row or row["status"] != "active":
                return None, False
            existing = (await conn.execute(text("SELECT * FROM mtapi_sync_requests WHERE connection_id = :id"), {"id": connection_id})).mappings().first()
            if existing and existing["state"] in ("queued", "running"):
                return dict(existing), False
            request = (await conn.execute(text("""
                INSERT INTO mtapi_sync_requests (connection_id, request_id, generation, state) VALUES (:id, 1, :g, 'queued')
                ON CONFLICT (connection_id) DO UPDATE SET request_id = mtapi_sync_requests.request_id + 1, generation = EXCLUDED.generation,
                    state = 'queued', detail = NULL, resources = '{}'::jsonb, attempts = 0, requested_at = NOW(), started_at = NULL, finished_at = NULL
                RETURNING *
            """), {"id": connection_id, "g": row["generation"]})).mappings().first()
            return dict(request), True

    async def get_sync_request(self, connection_id: int) -> dict | None:
        rows = await self._one("SELECT * FROM mtapi_sync_requests WHERE connection_id = :id", {"id": connection_id})
        return rows[0] if rows else None

    async def claim_sync_request(self, connection_id: int, generation: int, owner: str) -> dict | None:
        """Mark the pending request as running. Only the lease holder may do this, so a request
        left 'running' by a crashed pass is simply picked up again by the next lease holder."""
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            row = (await conn.execute(text("""
                UPDATE mtapi_sync_requests SET state = 'running', started_at = COALESCE(started_at, NOW()), attempts = attempts + 1
                WHERE connection_id = :id AND state IN ('queued', 'running') RETURNING *
            """), {"id": connection_id})).mappings().first()
        return dict(row) if row else None

    async def finish_sync_request(self, connection_id: int, request_id: int, state: str, detail: str | None, resources: dict) -> bool:
        """Record the outcome. Fenced by request_id: a newer request is never overwritten."""
        async with self.engine.begin() as conn:
            result = await conn.execute(text("""
                UPDATE mtapi_sync_requests SET state = :s, detail = :d, resources = CAST(:r AS jsonb), finished_at = NOW()
                WHERE connection_id = :id AND request_id = :rid AND state = 'running'
            """), {"id": connection_id, "rid": request_id, "s": state, "d": detail, "r": json.dumps(resources)})
        return bool(result.rowcount)

    async def has_queued_intervals(self, connection_id: int) -> bool:
        return bool(await self._one("SELECT 1 AS queued FROM mtapi_history_intervals WHERE connection_id = :id AND status = 'queued' LIMIT 1", {"id": connection_id}))

    # ── live state ───────────────────────────────────────────────────────────
    async def save_summary(self, connection_id: int, generation: int, owner: str, summary: schemas.AccountSummary, fetched_at: datetime) -> bool:
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            result = await conn.execute(text("""
                INSERT INTO mtapi_account_summary (connection_id, balance, credit, equity, margin, free_funds, margin_level, profit, fetched_at)
                VALUES (:id, :balance, :credit, :equity, :margin, :free_funds, :margin_level, :profit, :at)
                ON CONFLICT (connection_id) DO UPDATE SET balance = EXCLUDED.balance, credit = EXCLUDED.credit, equity = EXCLUDED.equity,
                    margin = EXCLUDED.margin, free_funds = EXCLUDED.free_funds, margin_level = EXCLUDED.margin_level,
                    profit = EXCLUDED.profit, fetched_at = EXCLUDED.fetched_at
                WHERE mtapi_account_summary.fetched_at < EXCLUDED.fetched_at
            """), {"id": connection_id, "balance": summary.balance, "credit": summary.credit, "equity": summary.equity, "margin": summary.margin,
                   "free_funds": summary.freeFunds, "margin_level": summary.marginLevel, "profit": summary.profit, "at": fetched_at})
            await self._touch_state(conn, connection_id, "summary", ok=True, snapshot_at=fetched_at)
        return bool(result.rowcount)

    async def replace_positions(self, connection_id: int, generation: int, owner: str, positions: list[schemas.OpenPosition], fetched_at: datetime) -> bool:
        """Replace the open-position set from one full, validated response. Older snapshots are ignored."""
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            newer = (await conn.execute(text("SELECT 1 FROM mtapi_sync_state WHERE connection_id = :id AND resource = 'positions' AND snapshot_at >= :at"),
                                        {"id": connection_id, "at": fetched_at})).first()
            if newer:
                return False
            await conn.execute(text("DELETE FROM mtapi_positions WHERE connection_id = :id"), {"id": connection_id})
            rows = _unique([{"eid": p.id, "symbol": p.symbol, "alias": p.alias, "side": p.side, "volume": p.volume, "open_price": p.openPrice,
                             "current_price": p.currentPrice, "commission": p.commission, "swap": p.swap, "profit": p.profit, "net_profit": p.netProfit,
                             "margin": p.margin, "open_time": _dt(p.openTime), "tp": p.tpPrice, "sl": p.slPrice, "trail": p.trailingDistance,
                             "comment": p.comment} for p in positions], "eid")
            for chunk in _chunks(rows):   # one statement per chunk instead of one round trip per row
                await conn.execute(text("""
                    INSERT INTO mtapi_positions (connection_id, external_id, symbol, alias, side, volume, open_price, current_price, commission, swap,
                        profit, net_profit, margin, open_time, tp_price, sl_price, trailing_distance, comment, fetched_at)
                    SELECT :id, x.eid, x.symbol, x.alias, x.side, x.volume, x.open_price, x.current_price, x.commission, x.swap,
                        x.profit, x.net_profit, x.margin, x.open_time, x.tp, x.sl, x.trail, x.comment, :at
                    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(eid text, symbol text, alias text, side text, volume numeric, open_price numeric,
                        current_price numeric, commission numeric, swap numeric, profit numeric, net_profit numeric, margin numeric,
                        open_time timestamptz, tp numeric, sl numeric, trail numeric, comment text)
                """), {"id": connection_id, "at": fetched_at, "rows": _rows_json(chunk)})
            await self._touch_state(conn, connection_id, "positions", ok=True, snapshot_at=fetched_at)
        return True

    async def replace_pending_orders(self, connection_id: int, generation: int, owner: str, orders: list[schemas.PendingOrder], fetched_at: datetime) -> bool:
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            newer = (await conn.execute(text("SELECT 1 FROM mtapi_sync_state WHERE connection_id = :id AND resource = 'pending_orders' AND snapshot_at >= :at"),
                                        {"id": connection_id, "at": fetched_at})).first()
            if newer:
                return False
            await conn.execute(text("DELETE FROM mtapi_pending_orders WHERE connection_id = :id"), {"id": connection_id})
            rows = _unique([{"eid": o.id, "symbol": o.symbol, "alias": o.alias, "side": o.side, "type": o.type, "volume": o.volume,
                             "activation": o.activationPrice, "tp": o.tpPrice, "sl": o.slPrice, "created": _dt(o.createTime),
                             "updated": _dt(o.lastUpdateTime), "comment": o.comment} for o in orders], "eid")
            for chunk in _chunks(rows):
                await conn.execute(text("""
                    INSERT INTO mtapi_pending_orders (connection_id, external_id, symbol, alias, side, order_type, volume, activation_price,
                        tp_price, sl_price, create_time, last_update_time, comment, fetched_at)
                    SELECT :id, x.eid, x.symbol, x.alias, x.side, x.type, x.volume, x.activation, x.tp, x.sl, x.created, x.updated, x.comment, :at
                    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(eid text, symbol text, alias text, side text, type text, volume numeric,
                        activation numeric, tp numeric, sl numeric, created timestamptz, updated timestamptz, comment text)
                """), {"id": connection_id, "at": fetched_at, "rows": _rows_json(chunk)})
            await self._touch_state(conn, connection_id, "pending_orders", ok=True, snapshot_at=fetched_at)
        return True

    # ── history ──────────────────────────────────────────────────────────────
    async def enqueue_interval(self, connection_id: int, resource: str, range_from: int, range_to: int, kind: str = "backfill") -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(text("""
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                VALUES (:id, :r, :f, :t, :k) ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
            """), {"id": connection_id, "r": resource, "f": range_from, "t": range_to, "k": kind})
        return bool(result.rowcount)

    async def enqueue_incremental(self, connection_id: int, generation: int, owner: str, resource: str, range_from: int, range_to: int) -> None:
        """Queue the next incremental window and advance the cursor in one transaction.

        The window is durable before the cursor moves, so a failed fetch is retried, never skipped."""
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            await conn.execute(text("""
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                VALUES (:id, :r, :f, :t, 'incremental') ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
            """), {"id": connection_id, "r": resource, "f": range_from, "t": range_to})
            await conn.execute(text("""
                INSERT INTO mtapi_sync_state (connection_id, resource, cursor_ms) VALUES (:id, :r, :cur)
                ON CONFLICT (connection_id, resource) DO UPDATE SET cursor_ms = GREATEST(COALESCE(mtapi_sync_state.cursor_ms, 0), EXCLUDED.cursor_ms)
            """), {"id": connection_id, "r": resource, "cur": range_to})

    async def enqueue_backfill(self, connection_id: int, resource: str, range_from: int, range_to: int) -> bool:
        """Queue a backfill from ``range_from``. A repeat request for the same start is a no-op."""
        async with self.engine.begin() as conn:
            existing = (await conn.execute(text("""
                SELECT 1 FROM mtapi_history_intervals
                WHERE connection_id = :id AND resource = :r AND kind = 'backfill' AND range_from = :f AND status <> 'cancelled' LIMIT 1
            """), {"id": connection_id, "r": resource, "f": range_from})).first()
            if existing:
                return False
            await conn.execute(text("""
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                VALUES (:id, :r, :f, :t, 'backfill') ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
            """), {"id": connection_id, "r": resource, "f": range_from, "t": range_to})
            # Start on it at the next worker round instead of waiting out the normal history interval.
            await conn.execute(text("UPDATE mtapi_connections SET next_history_sync_at = NOW(), history_requested_at = NOW() WHERE id = :id AND status = 'active'"), {"id": connection_id})
        return True

    async def request_older_history(self, connection_id: int, user_id: str, from_ms: int, to_ms: int, resources: list[str]) -> list[dict] | None:
        """Ask for history back to ``from_ms`` for a connection this user owns (one statement).

        Only the part that is not requested yet is queued: from ``from_ms`` up to the earliest
        backfill already requested for that resource (1 ms overlap on each side; rows are
        de-duplicated by uid). Nothing is queued when the range is already covered. A first
        request for a resource queues ``from_ms`` to ``to_ms``. Existing rows are never removed.
        Returns None when the connection is not this user's active connection; otherwise one
        entry per resource: {resource, queued, range_from, range_to}."""
        rows = await self._one("""
            WITH c AS (
                SELECT id FROM mtapi_connections WHERE id = :id AND user_id = :user AND status = 'active' FOR UPDATE),
            r AS (
                SELECT x.resource,
                       (SELECT MIN(h.range_from) FROM mtapi_history_intervals h
                        WHERE h.connection_id = :id AND h.resource = x.resource AND h.kind = 'backfill' AND h.status <> 'cancelled') AS earliest
                FROM c, jsonb_array_elements_text(CAST(:resources AS jsonb)) AS x(resource)),
            plan AS (
                SELECT r.resource, CAST(:from_ms AS bigint) - 1 AS range_from,
                       CASE WHEN r.earliest IS NULL THEN CAST(:to_ms AS bigint) + 1 ELSE r.earliest + 1 END AS range_to
                FROM r WHERE r.earliest IS NULL OR CAST(:from_ms AS bigint) - 1 < r.earliest),
            ins AS (
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                SELECT CAST(:id AS integer), plan.resource, plan.range_from, plan.range_to, 'backfill' FROM plan WHERE plan.range_from < plan.range_to
                ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
                RETURNING resource, range_from, range_to),
            wake AS (
                UPDATE mtapi_connections SET next_history_sync_at = NOW(), history_requested_at = NOW() WHERE id = :id AND EXISTS (SELECT 1 FROM ins) RETURNING 1)
            SELECT r.resource, (i.resource IS NOT NULL) AS queued, i.range_from, i.range_to, r.earliest
            FROM r LEFT JOIN ins i ON i.resource = r.resource
            UNION ALL SELECT NULL, FALSE, NULL, NULL, NULL FROM c WHERE NOT EXISTS (SELECT 1 FROM r)
        """, {"id": connection_id, "user": user_id, "from_ms": int(from_ms), "to_ms": int(to_ms), "resources": json.dumps(resources)})
        if not rows:
            return None
        return [row for row in rows if row["resource"] is not None]

    async def save_account_created(self, connection_id: int, generation: int, owner: str, created_ms: int | None) -> None:
        """Fill in the creation time once, for connections stored before it was recorded."""
        await self._one(f"""
            WITH {_GUARD}
            UPDATE mtapi_connections c SET account_created_at = to_timestamp(CAST(:ms AS bigint) / 1000.0)
            FROM g WHERE c.id = g.id AND c.account_created_at IS NULL AND CAST(:ms AS bigint) IS NOT NULL RETURNING 1
        """, {"id": connection_id, "gen": generation, "o": owner, "ms": plausible_creation_ms(created_ms)})

    async def enqueue_reconcile_if_due(self, connection_id: int, resource: str, range_from: int, range_to: int, every_seconds: int) -> bool:
        """Periodically re-read a wider recent window so late corrections outside the overlap are picked up."""
        async with self.engine.begin() as conn:
            recent = (await conn.execute(text("""
                SELECT 1 FROM mtapi_history_intervals
                WHERE connection_id = :id AND resource = :r AND kind = 'reconcile' AND created_at > NOW() - make_interval(secs => :every) LIMIT 1
            """), {"id": connection_id, "r": resource, "every": every_seconds})).first()
            if recent:
                return False
            result = await conn.execute(text("""
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                VALUES (:id, :r, :f, :t, 'reconcile') ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
            """), {"id": connection_id, "r": resource, "f": range_from, "t": range_to})
        return bool(result.rowcount)

    async def next_intervals(self, connection_id: int, resource: str, limit: int) -> list[dict]:
        """Incremental (recent) windows first, then backfill newest-first."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT * FROM mtapi_history_intervals
                WHERE connection_id = :id AND resource = :r AND status = 'queued'
                ORDER BY (kind = 'incremental') DESC, range_to DESC, id LIMIT :n
            """), {"id": connection_id, "r": resource, "n": limit})).mappings().all()
        return [dict(r) for r in rows]

    @staticmethod
    def _closed_rows(entries: list) -> list[dict]:
        return _unique([{"uid": e.uid, "pid": e.id, "coid": e.closingOrderId, "symbol": e.symbol, "alias": e.alias, "side": e.side,
                         "volume": e.volume, "open_price": e.openPrice, "close_price": e.closePrice, "commission": e.commission, "swap": e.swap,
                         "profit": e.profit, "net_profit": e.netProfit, "open_time": _dt(e.openTime), "close_time": _dt(e.closeTime), "sl": e.slPrice,
                         "tp": e.tpPrice, "reason": e.closeReason, "comment": e.comment, "raw": json.loads(_raw(e))} for e in entries], "uid")

    @staticmethod
    def _ledger_rows(entries: list) -> list[dict]:
        return _unique([{"eid": e.id, "type": e.type, "amount": e.amount, "at": _dt(e.timestamp), "comment": e.comment,
                         "raw": json.loads(_raw(e))} for e in entries], "eid")

    async def complete_interval(self, connection_id: int, generation: int, owner: str, interval: dict, entries: list, verdict: str,
                                children: list[tuple[int, int]], *, ttl_seconds: int | None = None) -> None:
        """Atomically (one statement): store the rows, record the range's outcome, queue any
        sub-ranges, mark the resource as synced and renew the lease.

        If the statement fails nothing of it is kept and the range stays queued (it is replayed);
        once it succeeds the range is never fetched again."""
        resource = interval["resource"]
        if resource == "closed_positions":
            rows = self._closed_rows(entries)
            upsert = """
                INSERT INTO mtapi_closed_positions (connection_id, uid, position_id, closing_order_id, symbol, alias, side, volume, open_price,
                    close_price, commission, swap, profit, net_profit, open_time, close_time, sl_price, tp_price, close_reason, comment, raw)
                SELECT CAST(:id AS integer), x.uid, x.pid, x.coid, x.symbol, x.alias, x.side, x.volume, x.open_price, x.close_price, x.commission,
                    x.swap, x.profit, x.net_profit, x.open_time, x.close_time, x.sl, x.tp, x.reason, x.comment, x.raw
                FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(uid text, pid text, coid text, symbol text, alias text, side text, volume numeric,
                    open_price numeric, close_price numeric, commission numeric, swap numeric, profit numeric, net_profit numeric,
                    open_time timestamptz, close_time timestamptz, sl numeric, tp numeric, reason text, comment text, raw jsonb)
                WHERE EXISTS (SELECT 1 FROM g)
                ON CONFLICT (connection_id, uid) DO UPDATE SET
                    previous_raw = CASE WHEN mtapi_closed_positions.raw IS DISTINCT FROM EXCLUDED.raw THEN mtapi_closed_positions.raw ELSE mtapi_closed_positions.previous_raw END,
                    revision = mtapi_closed_positions.revision + CASE WHEN mtapi_closed_positions.raw IS DISTINCT FROM EXCLUDED.raw THEN 1 ELSE 0 END,
                    position_id = EXCLUDED.position_id, closing_order_id = EXCLUDED.closing_order_id, symbol = EXCLUDED.symbol, alias = EXCLUDED.alias,
                    side = EXCLUDED.side, volume = EXCLUDED.volume, open_price = EXCLUDED.open_price, close_price = EXCLUDED.close_price,
                    commission = EXCLUDED.commission, swap = EXCLUDED.swap, profit = EXCLUDED.profit, net_profit = EXCLUDED.net_profit,
                    open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time, sl_price = EXCLUDED.sl_price, tp_price = EXCLUDED.tp_price,
                    close_reason = EXCLUDED.close_reason, comment = EXCLUDED.comment, raw = EXCLUDED.raw, last_seen_at = NOW()
                RETURNING 1"""
        else:
            rows = self._ledger_rows(entries)
            upsert = """
                INSERT INTO mtapi_ledgers (connection_id, external_id, entry_type, amount, entry_time, comment, raw)
                SELECT CAST(:id AS integer), x.eid, x.type, x.amount, x.at, x.comment, x.raw
                FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(eid text, type text, amount numeric, at timestamptz, comment text, raw jsonb)
                WHERE EXISTS (SELECT 1 FROM g)
                ON CONFLICT (connection_id, external_id) DO UPDATE SET
                    previous_raw = CASE WHEN mtapi_ledgers.raw IS DISTINCT FROM EXCLUDED.raw THEN mtapi_ledgers.raw ELSE mtapi_ledgers.previous_raw END,
                    revision = mtapi_ledgers.revision + CASE WHEN mtapi_ledgers.raw IS DISTINCT FROM EXCLUDED.raw THEN 1 ELSE 0 END,
                    entry_type = EXCLUDED.entry_type, amount = EXCLUDED.amount, entry_time = EXCLUDED.entry_time,
                    comment = EXCLUDED.comment, raw = EXCLUDED.raw, last_seen_at = NOW()
                RETURNING 1"""
        status = {COMPLETE: "done", SPLIT: "split", GAP: "gap", WINDOW: "split_window"}[verdict]
        result = await self._one(f"""
            WITH {_GUARD},
            up AS ({upsert}),
            iv AS (
                UPDATE mtapi_history_intervals SET status = CAST(:status AS text), records = CAST(:n AS integer), completed_at = NOW()
                WHERE id = :iid AND status = 'queued' AND EXISTS (SELECT 1 FROM g) RETURNING 1),
            ch AS (
                INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                SELECT CAST(:id AS integer), CAST(:resource AS text), x.f, x.t, CAST(:kind AS text)
                FROM jsonb_to_recordset(CAST(:children AS jsonb)) AS x(f bigint, t bigint)
                WHERE EXISTS (SELECT 1 FROM g)
                ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING RETURNING 1),
            st AS ({_STATE_UPSERT}),
            lease AS (
                UPDATE mtapi_connections c SET lease_expires_at = GREATEST(c.lease_expires_at, NOW() + make_interval(secs => CAST(:ttl AS double precision)))
                FROM g WHERE c.id = g.id RETURNING 1)
            SELECT EXISTS (SELECT 1 FROM g) AS ok
        """, {"id": connection_id, "gen": generation, "o": owner, "rows": _rows_json(rows), "status": status, "n": len(entries),
              "iid": interval["id"], "resource": resource, "kind": interval["kind"], "children": json.dumps([{"f": lo, "t": hi} for lo, hi in children]),
              "states": json.dumps([{"resource": resource, "ok": True, "err": None, "snap": None}]), "ttl": ttl_seconds or 0})
        if not result or not result[0]["ok"]:
            raise StaleWriter()

    async def coverage(self, connection_id: int) -> dict[str, dict]:
        """What is actually known about history completeness, per resource."""
        out: dict[str, dict] = {}
        async with self.engine.connect() as conn:
            for resource in HISTORY_RESOURCES:
                row = (await conn.execute(text("""
                    SELECT COUNT(*) FILTER (WHERE status = 'queued' AND kind = 'backfill') AS queued,   -- the requested import only
                           COUNT(*) FILTER (WHERE status = 'gap') AS gaps,
                           MIN(range_from) FILTER (WHERE kind = 'backfill') AS requested_from_ms,
                           MIN(range_from) FILTER (WHERE status = 'done') AS oldest_done_from_ms,
                           MAX(range_to) FILTER (WHERE status = 'done') AS newest_done_to_ms
                    FROM mtapi_history_intervals WHERE connection_id = :id AND resource = :r
                """), {"id": connection_id, "r": resource})).mappings().first()
                gaps = (await conn.execute(text("SELECT range_from, range_to FROM mtapi_history_intervals WHERE connection_id = :id AND resource = :r AND status = 'gap' ORDER BY range_from LIMIT 50"),
                                           {"id": connection_id, "r": resource})).all()
                table = "mtapi_closed_positions" if resource == "closed_positions" else "mtapi_ledgers"
                count = (await conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE connection_id = :id"), {"id": connection_id})).scalar()
                requested = row["requested_from_ms"] is not None
                complete = requested and row["queued"] == 0 and row["gaps"] == 0
                out[resource] = {"records": int(count or 0), "queued_intervals": int(row["queued"]), "gap_count": int(row["gaps"]),
                                 "gaps_ms": [[int(a), int(b)] for a, b in gaps], "requested_from_ms": row["requested_from_ms"],
                                 "oldest_covered_from_ms": row["oldest_done_from_ms"], "newest_covered_to_ms": row["newest_done_to_ms"],
                                 "status": "complete_for_requested_range" if complete else ("partial_gaps" if row["gaps"] else ("in_progress" if row["queued"] else "not_requested"))}
        return out

    async def closed_positions_for_report(self, connection_id: int) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT uid, position_id, symbol, side, volume, close_price, net_profit, profit, close_time
                FROM mtapi_closed_positions WHERE connection_id = :id ORDER BY close_time
            """), {"id": connection_id})).mappings().all()
        return [dict(r) for r in rows]

    async def legacy_trades_for_report(self, external_account_id: str) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, symbol, direction, volume, close_price, pnl, closed_at, source FROM trades
                WHERE account_id = :a AND (source = 'scraper' OR source IS NULL) ORDER BY closed_at
            """), {"a": external_account_id})).mappings().all()
        return [dict(r) for r in rows]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
