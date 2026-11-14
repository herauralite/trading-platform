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
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services.matchtrader_api import schemas
from app.services.matchtrader_api.backfill import COMPLETE, GAP, SPLIT

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


def _raw(model) -> str:
    return json.dumps(model.raw, default=str, sort_keys=True)


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
]

DROP_ORDER = ["mtapi_history_intervals", "mtapi_ledgers", "mtapi_closed_positions", "mtapi_pending_orders",
              "mtapi_positions", "mtapi_account_summary", "mtapi_sync_state", "mtapi_credentials", "mtapi_connections"]


class SqlStore:
    def __init__(self, engine_getter: Callable[[], Any] = _default_engine):
        self._engine_getter = engine_getter

    @property
    def engine(self):
        return self._engine_getter()

    async def ensure_schema(self) -> None:
        async with self.engine.begin() as conn:
            for statement in DDL:
                await conn.execute(text(statement))

    # ── connections ──────────────────────────────────────────────────────────
    async def create_connection(self, *, user_id: str, identity: dict, capabilities: dict, status: str, status_detail: str | None,
                                linked_external_account_id: str | None, token_hint: str, encrypt: Callable[[int], str], key_version: Callable[[str], str]) -> dict:
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(text("""
                    INSERT INTO mtapi_connections (user_id, platform_host, external_login, broker_name, platform_url,
                        account_currency, account_type, status, status_detail, capabilities, linked_external_account_id, token_hint)
                    VALUES (:user_id, :platform_host, :external_login, :broker_name, :platform_url,
                        :account_currency, :account_type, :status, :status_detail, CAST(:capabilities AS jsonb), :linked, :token_hint)
                    RETURNING *
                """), {**identity, "user_id": user_id, "status": status, "status_detail": status_detail,
                       "capabilities": json.dumps(capabilities), "linked": linked_external_account_id, "token_hint": token_hint})).mappings().first()
                blob = encrypt(row["id"])
                await conn.execute(text("INSERT INTO mtapi_credentials (connection_id, ciphertext, key_version, generation) VALUES (:id, :c, :k, :g)"),
                                   {"id": row["id"], "c": blob, "k": key_version(blob), "g": row["generation"]})
                return dict(row)
        except IntegrityError:
            raise IdentityConflict() from None

    async def get_connection(self, connection_id: int) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM mtapi_connections WHERE id = :id"), {"id": connection_id})).mappings().first()
        return dict(row) if row else None

    async def list_connections(self, user_id: str) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM mtapi_connections WHERE user_id = :u AND status <> 'disconnected' ORDER BY id"), {"u": user_id})).mappings().all()
        return [dict(r) for r in rows]

    async def get_credential(self, connection_id: int) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT ciphertext, key_version, generation FROM mtapi_credentials WHERE connection_id = :id"), {"id": connection_id})).mappings().first()
        return dict(row) if row else None

    async def set_status(self, connection_id: int, status: str, detail: str | None = None, *, bump_generation: bool = True, only_from: tuple[str, ...] | None = None) -> bool:
        """Change status. Bumping the generation fences every in-flight writer."""
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
        return bool(result.rowcount)

    async def rotate_credential(self, connection_id: int, *, encrypt: Callable[[int], str], key_version: Callable[[str], str], token_hint: str, capabilities: dict) -> dict:
        async with self.engine.begin() as conn:
            row = (await conn.execute(text("""
                UPDATE mtapi_connections
                SET generation = generation + 1, status = 'active', status_detail = NULL, token_hint = :hint,
                    capabilities = CAST(:caps AS jsonb), lease_owner = NULL, lease_expires_at = NULL,
                    next_live_sync_at = NOW(), next_history_sync_at = NOW(), updated_at = NOW()
                WHERE id = :id AND status <> 'disconnected' RETURNING *
            """), {"id": connection_id, "hint": token_hint, "caps": json.dumps(capabilities)})).mappings().first()
            if not row:
                raise StaleWriter()
            blob = encrypt(connection_id)
            await conn.execute(text("""
                INSERT INTO mtapi_credentials (connection_id, ciphertext, key_version, generation) VALUES (:id, :c, :k, :g)
                ON CONFLICT (connection_id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, key_version = EXCLUDED.key_version,
                    generation = EXCLUDED.generation, created_at = NOW()
            """), {"id": connection_id, "c": blob, "k": key_version(blob), "g": row["generation"]})
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
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, next_live_sync_at <= NOW() AS live_due, next_history_sync_at <= NOW() AS history_due
                FROM mtapi_connections
                WHERE status = 'active' AND (next_live_sync_at <= NOW() OR next_history_sync_at <= NOW())
                  AND (lease_owner IS NULL OR lease_expires_at < NOW())
                ORDER BY LEAST(next_live_sync_at, next_history_sync_at) LIMIT :n
            """), {"n": limit})).mappings().all()
        return [dict(r) for r in rows]

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
            for p in positions:
                await conn.execute(text("""
                    INSERT INTO mtapi_positions (connection_id, external_id, symbol, alias, side, volume, open_price, current_price, commission, swap,
                        profit, net_profit, margin, open_time, tp_price, sl_price, trailing_distance, comment, fetched_at)
                    VALUES (:id, :eid, :symbol, :alias, :side, :volume, :open_price, :current_price, :commission, :swap,
                        :profit, :net_profit, :margin, :open_time, :tp, :sl, :trail, :comment, :at)
                """), {"id": connection_id, "eid": p.id, "symbol": p.symbol, "alias": p.alias, "side": p.side, "volume": p.volume,
                       "open_price": p.openPrice, "current_price": p.currentPrice, "commission": p.commission, "swap": p.swap, "profit": p.profit,
                       "net_profit": p.netProfit, "margin": p.margin, "open_time": _dt(p.openTime), "tp": p.tpPrice, "sl": p.slPrice,
                       "trail": p.trailingDistance, "comment": p.comment, "at": fetched_at})
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
            for o in orders:
                await conn.execute(text("""
                    INSERT INTO mtapi_pending_orders (connection_id, external_id, symbol, alias, side, order_type, volume, activation_price,
                        tp_price, sl_price, create_time, last_update_time, comment, fetched_at)
                    VALUES (:id, :eid, :symbol, :alias, :side, :type, :volume, :activation, :tp, :sl, :created, :updated, :comment, :at)
                """), {"id": connection_id, "eid": o.id, "symbol": o.symbol, "alias": o.alias, "side": o.side, "type": o.type, "volume": o.volume,
                       "activation": o.activationPrice, "tp": o.tpPrice, "sl": o.slPrice, "created": _dt(o.createTime), "updated": _dt(o.lastUpdateTime),
                       "comment": o.comment, "at": fetched_at})
            await self._touch_state(conn, connection_id, "pending_orders", ok=True, snapshot_at=fetched_at)
        return True

    # ── history ──────────────────────────────────────────────────────────────
    async def _upsert_closed(self, conn, connection_id: int, entries: list[schemas.ClosedPosition]) -> None:
        for e in entries:
            await conn.execute(text("""
                INSERT INTO mtapi_closed_positions (connection_id, uid, position_id, closing_order_id, symbol, alias, side, volume, open_price,
                    close_price, commission, swap, profit, net_profit, open_time, close_time, sl_price, tp_price, close_reason, comment, raw)
                VALUES (:id, :uid, :pid, :coid, :symbol, :alias, :side, :volume, :open_price, :close_price, :commission, :swap, :profit,
                    :net_profit, :open_time, :close_time, :sl, :tp, :reason, :comment, CAST(:raw AS jsonb))
                ON CONFLICT (connection_id, uid) DO UPDATE SET
                    previous_raw = CASE WHEN mtapi_closed_positions.raw IS DISTINCT FROM EXCLUDED.raw THEN mtapi_closed_positions.raw ELSE mtapi_closed_positions.previous_raw END,
                    revision = mtapi_closed_positions.revision + CASE WHEN mtapi_closed_positions.raw IS DISTINCT FROM EXCLUDED.raw THEN 1 ELSE 0 END,
                    position_id = EXCLUDED.position_id, closing_order_id = EXCLUDED.closing_order_id, symbol = EXCLUDED.symbol, alias = EXCLUDED.alias,
                    side = EXCLUDED.side, volume = EXCLUDED.volume, open_price = EXCLUDED.open_price, close_price = EXCLUDED.close_price,
                    commission = EXCLUDED.commission, swap = EXCLUDED.swap, profit = EXCLUDED.profit, net_profit = EXCLUDED.net_profit,
                    open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time, sl_price = EXCLUDED.sl_price, tp_price = EXCLUDED.tp_price,
                    close_reason = EXCLUDED.close_reason, comment = EXCLUDED.comment, raw = EXCLUDED.raw, last_seen_at = NOW()
            """), {"id": connection_id, "uid": e.uid, "pid": e.id, "coid": e.closingOrderId, "symbol": e.symbol, "alias": e.alias, "side": e.side,
                   "volume": e.volume, "open_price": e.openPrice, "close_price": e.closePrice, "commission": e.commission, "swap": e.swap,
                   "profit": e.profit, "net_profit": e.netProfit, "open_time": _dt(e.openTime), "close_time": _dt(e.closeTime), "sl": e.slPrice,
                   "tp": e.tpPrice, "reason": e.closeReason, "comment": e.comment, "raw": _raw(e)})

    async def _upsert_ledgers(self, conn, connection_id: int, entries: list[schemas.LedgerEntry]) -> None:
        for e in entries:
            await conn.execute(text("""
                INSERT INTO mtapi_ledgers (connection_id, external_id, entry_type, amount, entry_time, comment, raw)
                VALUES (:id, :eid, :type, :amount, :at, :comment, CAST(:raw AS jsonb))
                ON CONFLICT (connection_id, external_id) DO UPDATE SET
                    previous_raw = CASE WHEN mtapi_ledgers.raw IS DISTINCT FROM EXCLUDED.raw THEN mtapi_ledgers.raw ELSE mtapi_ledgers.previous_raw END,
                    revision = mtapi_ledgers.revision + CASE WHEN mtapi_ledgers.raw IS DISTINCT FROM EXCLUDED.raw THEN 1 ELSE 0 END,
                    entry_type = EXCLUDED.entry_type, amount = EXCLUDED.amount, entry_time = EXCLUDED.entry_time,
                    comment = EXCLUDED.comment, raw = EXCLUDED.raw, last_seen_at = NOW()
            """), {"id": connection_id, "eid": e.id, "type": e.type, "amount": e.amount, "at": _dt(e.timestamp), "comment": e.comment, "raw": _raw(e)})

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
        return True

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

    async def complete_interval(self, connection_id: int, generation: int, owner: str, interval: dict, entries: list, verdict: str,
                                children: list[tuple[int, int]]) -> None:
        """Atomically: store the rows, record the interval outcome, queue any sub-ranges.

        A crash before commit leaves the interval queued (it is replayed); after commit it is never refetched."""
        resource = interval["resource"]
        async with self.engine.begin() as conn:
            await self._guard(conn, connection_id, generation, owner)
            if resource == "closed_positions":
                await self._upsert_closed(conn, connection_id, entries)
            else:
                await self._upsert_ledgers(conn, connection_id, entries)
            status = {COMPLETE: "done", SPLIT: "split", GAP: "gap"}[verdict]
            await conn.execute(text("UPDATE mtapi_history_intervals SET status = :s, records = :n, completed_at = NOW() WHERE id = :iid AND status = 'queued'"),
                               {"s": status, "n": len(entries), "iid": interval["id"]})
            for lo, hi in children:
                await conn.execute(text("""
                    INSERT INTO mtapi_history_intervals (connection_id, resource, range_from, range_to, kind)
                    VALUES (:id, :r, :f, :t, :k) ON CONFLICT (connection_id, resource, range_from, range_to) DO NOTHING
                """), {"id": connection_id, "r": resource, "f": lo, "t": hi, "k": interval["kind"]})
            await self._touch_state(conn, connection_id, resource, ok=True)

    async def coverage(self, connection_id: int) -> dict[str, dict]:
        """What is actually known about history completeness, per resource."""
        out: dict[str, dict] = {}
        async with self.engine.connect() as conn:
            for resource in HISTORY_RESOURCES:
                row = (await conn.execute(text("""
                    SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued,
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
