from datetime import datetime, timezone, timedelta
from typing import Any
import hashlib
import json

from sqlalchemy import text

from app.core.database import engine

SNAPSHOT_DEDUPE_WINDOW_SECONDS = 30
USER_SCOPED_CONNECTORS = {"manual", "csv_import"}


def _normalize_connector(value: str | None) -> str:
    return (value or "manual").strip().lower().replace("-", "_")


def _normalize_user_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _parse_dt(value: Any):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def compute_account_key(connector_type: str | None, user_id: str | None, external_account_id: str | None) -> str:
    connector = _normalize_connector(connector_type)
    external = str(external_account_id or "").strip()
    if not external:
        raise ValueError("external_account_id is required")

    normalized_user = _normalize_user_id(user_id)
    owner_scope = normalized_user if connector in USER_SCOPED_CONNECTORS else "global"
    raw = f"{connector}|{owner_scope}|{external}"
    return hashlib.sha256(raw.encode()).hexdigest()


def compute_position_key(symbol: str | None, side: str | None, opened_at: Any = None) -> str:
    opened = _parse_dt(opened_at)
    opened_part = opened.isoformat() if opened else "na"
    return f"{(symbol or '').upper()}|{(side or '').lower()}|{opened_part}"


async def ensure_connector_tables() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trading_accounts (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                connector_type TEXT NOT NULL,
                broker_name TEXT,
                external_account_id TEXT NOT NULL,
                account_key TEXT,
                display_label TEXT,
                account_type TEXT,
                account_size INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, connector_type, external_account_id)
            )
        """))
        await conn.execute(text("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS account_key TEXT"))

        rows = (await conn.execute(text("""
            SELECT id, connector_type, user_id, external_account_id
            FROM trading_accounts
            WHERE account_key IS NULL
        """))).mappings().all()
        for row in rows:
            await conn.execute(text("""
                UPDATE trading_accounts
                SET account_key = :account_key
                WHERE id = :id
            """), {
                "id": row["id"],
                "account_key": compute_account_key(row["connector_type"], row["user_id"], row["external_account_id"]),
            })

        # deterministic dedup by account_key before creating unique index
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id, account_key,
                       ROW_NUMBER() OVER (PARTITION BY account_key ORDER BY updated_at DESC, id DESC) AS rn,
                       MAX(id) OVER (PARTITION BY account_key) AS keep_id
                FROM trading_accounts
                WHERE account_key IS NOT NULL
            )
            UPDATE account_snapshots s
            SET trading_account_id = r.keep_id
            FROM ranked r
            WHERE s.trading_account_id = r.id AND r.rn > 1
        """))
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id, account_key,
                       ROW_NUMBER() OVER (PARTITION BY account_key ORDER BY updated_at DESC, id DESC) AS rn,
                       MAX(id) OVER (PARTITION BY account_key) AS keep_id
                FROM trading_accounts
                WHERE account_key IS NOT NULL
            )
            UPDATE positions p
            SET trading_account_id = r.keep_id
            FROM ranked r
            WHERE p.trading_account_id = r.id AND r.rn > 1
        """))
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id, account_key,
                       ROW_NUMBER() OVER (PARTITION BY account_key ORDER BY updated_at DESC, id DESC) AS rn,
                       MAX(id) OVER (PARTITION BY account_key) AS keep_id
                FROM trading_accounts
                WHERE account_key IS NOT NULL
            )
            UPDATE connector_events e
            SET trading_account_id = r.keep_id
            FROM ranked r
            WHERE e.trading_account_id = r.id AND r.rn > 1
        """))
        await conn.execute(text("""
            DELETE FROM trading_accounts t
            USING (
                SELECT id,
                       ROW_NUMBER() OVER (PARTITION BY account_key ORDER BY updated_at DESC, id DESC) AS rn
                FROM trading_accounts
                WHERE account_key IS NOT NULL
            ) d
            WHERE t.id = d.id AND d.rn > 1
        """))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS trading_accounts_account_key_uq ON trading_accounts(account_key)"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id SERIAL PRIMARY KEY,
                trading_account_id INTEGER REFERENCES trading_accounts(id) ON DELETE CASCADE,
                snapshot_time TIMESTAMPTZ NOT NULL,
                balance DOUBLE PRECISION,
                equity DOUBLE PRECISION,
                drawdown DOUBLE PRECISION,
                risk_used DOUBLE PRECISION,
                source_metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS account_snapshots_account_time_idx ON account_snapshots(trading_account_id, snapshot_time DESC)"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                trading_account_id INTEGER REFERENCES trading_accounts(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                side TEXT,
                size DOUBLE PRECISION,
                average_entry DOUBLE PRECISION,
                unrealized_pnl DOUBLE PRECISION,
                stop_loss DOUBLE PRECISION,
                take_profit DOUBLE PRECISION,
                opened_at TIMESTAMPTZ,
                position_key TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                closed_at TIMESTAMPTZ,
                source_metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (trading_account_id, symbol, side)
            )
        """))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS position_key TEXT"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT NOW()"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS positions_account_position_key_uq ON positions(trading_account_id, position_key) WHERE position_key IS NOT NULL"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS connector_events (
                id SERIAL PRIMARY KEY,
                trading_account_id INTEGER REFERENCES trading_accounts(id) ON DELETE SET NULL,
                user_id TEXT,
                connector_type TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_payload JSONB DEFAULT '{}'::jsonb,
                event_time TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))

        # Preserve richer canonical trade ingestion data without breaking existing readers.
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS connector_type TEXT"))
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS open_time TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS fees DOUBLE PRECISION"))
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb"))
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS source_metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS import_provenance JSONB DEFAULT '{}'::jsonb"))


async def upsert_trading_account(account: dict[str, Any]) -> dict[str, Any]:
    connector_type = _normalize_connector(account.get("connector_type") or account.get("source_connector"))
    user_id = _normalize_user_id(account.get("user_id"))
    external_account_id = str(account.get("external_account_id") or "").strip()
    account_key = compute_account_key(connector_type, user_id, external_account_id)

    params = {
        "account_key": account_key,
        "user_id": user_id,
        "connector_type": connector_type,
        "broker_name": account.get("broker_name"),
        "external_account_id": external_account_id,
        "display_label": account.get("display_label"),
        "account_type": account.get("account_type"),
        "account_size": account.get("account_size"),
        "is_active": account.get("is_active", True),
        "metadata": json.dumps(account.get("metadata") or {}),
    }
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO trading_accounts (
                account_key, user_id, connector_type, broker_name, external_account_id,
                display_label, account_type, account_size, is_active, metadata
            ) VALUES (
                :account_key, :user_id, :connector_type, :broker_name, :external_account_id,
                :display_label, :account_type, :account_size, :is_active, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (account_key)
            DO UPDATE SET
                user_id = COALESCE(EXCLUDED.user_id, trading_accounts.user_id),
                broker_name = COALESCE(EXCLUDED.broker_name, trading_accounts.broker_name),
                display_label = COALESCE(EXCLUDED.display_label, trading_accounts.display_label),
                account_type = COALESCE(EXCLUDED.account_type, trading_accounts.account_type),
                account_size = COALESCE(EXCLUDED.account_size, trading_accounts.account_size),
                is_active = EXCLUDED.is_active,
                metadata = trading_accounts.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING *
        """), params)).mappings().first()
    return dict(row)


async def ingest_account_snapshot(payload: dict[str, Any]) -> bool:
    account = await upsert_trading_account(payload)
    snapshot_time = _parse_dt(payload.get("timestamp")) or datetime.now(timezone.utc)
    async with engine.begin() as conn:
        latest = (await conn.execute(text("""
            SELECT snapshot_time, balance, equity, drawdown, risk_used
            FROM account_snapshots
            WHERE trading_account_id = :aid
            ORDER BY snapshot_time DESC
            LIMIT 1
        """), {"aid": account["id"]})).mappings().first()

        if latest:
            within_window = latest["snapshot_time"] and latest["snapshot_time"] >= snapshot_time - timedelta(seconds=SNAPSHOT_DEDUPE_WINDOW_SECONDS)
            unchanged = (
                latest["balance"] == payload.get("balance")
                and latest["equity"] == payload.get("equity")
                and latest["drawdown"] == payload.get("drawdown")
                and latest["risk_used"] == payload.get("risk_used")
            )
            if within_window and unchanged:
                return False

        await conn.execute(text("""
            INSERT INTO account_snapshots (
                trading_account_id, snapshot_time, balance, equity, drawdown, risk_used, source_metadata
            ) VALUES (
                :trading_account_id, :snapshot_time, :balance, :equity, :drawdown, :risk_used, CAST(:source_metadata AS jsonb)
            )
        """), {
            "trading_account_id": account["id"],
            "snapshot_time": snapshot_time,
            "balance": payload.get("balance"),
            "equity": payload.get("equity"),
            "drawdown": payload.get("drawdown"),
            "risk_used": payload.get("risk_used"),
            "source_metadata": json.dumps(payload.get("source_metadata") or {}),
        })
    return True


async def ingest_position(payload: dict[str, Any]) -> str:
    account = await upsert_trading_account(payload)
    now = datetime.now(timezone.utc)
    position_key = payload.get("position_key") or compute_position_key(payload.get("symbol"), payload.get("side"), payload.get("opened_at"))
    params = {
        "trading_account_id": account["id"],
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "size": payload.get("size"),
        "average_entry": payload.get("average_entry"),
        "unrealized_pnl": payload.get("unrealized_pnl"),
        "stop_loss": payload.get("stop_loss"),
        "take_profit": payload.get("take_profit"),
        "opened_at": _parse_dt(payload.get("opened_at")),
        "position_key": position_key,
        "last_seen_at": now,
        "source_metadata": json.dumps(payload.get("source_metadata") or {}),
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO positions (
                trading_account_id, symbol, side, size, average_entry, unrealized_pnl,
                stop_loss, take_profit, opened_at, position_key,
                is_active, last_seen_at, closed_at, source_metadata
            ) VALUES (
                :trading_account_id, :symbol, :side, :size, :average_entry, :unrealized_pnl,
                :stop_loss, :take_profit, :opened_at, :position_key,
                TRUE, :last_seen_at, NULL, CAST(:source_metadata AS jsonb)
            )
            ON CONFLICT (trading_account_id, position_key)
            DO UPDATE SET
                symbol = EXCLUDED.symbol,
                side = EXCLUDED.side,
                size = EXCLUDED.size,
                average_entry = EXCLUDED.average_entry,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                stop_loss = EXCLUDED.stop_loss,
                take_profit = EXCLUDED.take_profit,
                opened_at = COALESCE(EXCLUDED.opened_at, positions.opened_at),
                is_active = TRUE,
                last_seen_at = EXCLUDED.last_seen_at,
                closed_at = NULL,
                source_metadata = positions.source_metadata || EXCLUDED.source_metadata,
                updated_at = NOW()
        """), params)
    return position_key


async def deactivate_missing_positions(trading_account_id: int, seen_position_keys: list[str], allow_empty_snapshot: bool = False) -> int:
    seen_position_keys = [k for k in seen_position_keys if k]
    if not seen_position_keys and not allow_empty_snapshot:
        return 0

    async with engine.begin() as conn:
        if seen_position_keys:
            result = await conn.execute(text("""
                UPDATE positions
                SET is_active = FALSE,
                    closed_at = COALESCE(closed_at, NOW()),
                    updated_at = NOW()
                WHERE trading_account_id = :aid
                  AND is_active = TRUE
                  AND (position_key IS NULL OR NOT (position_key = ANY(:keys)))
            """), {"aid": trading_account_id, "keys": seen_position_keys})
        else:
            result = await conn.execute(text("""
                UPDATE positions
                SET is_active = FALSE,
                    closed_at = COALESCE(closed_at, NOW()),
                    updated_at = NOW()
                WHERE trading_account_id = :aid
                  AND is_active = TRUE
            """), {"aid": trading_account_id})
    return result.rowcount or 0


async def ingest_trade(payload: dict[str, Any]) -> bool:
    account = await upsert_trading_account(payload)
    account_size = payload.get("account_size") or account.get("account_size") or 10000
    pnl = payload.get("pnl") or 0
    if abs(pnl) > account_size:
        return False

    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO trades (
                account_id, account_type, account_size,
                symbol, direction, volume, open_price, close_price, pnl,
                balance_after, equity_after,
                daily_loss_used, daily_loss_limit,
                overall_loss_used, overall_loss_limit, closed_at, source,
                connector_type, open_time, fees, tags, source_metadata, import_provenance
            ) VALUES (
                :account_id, :account_type, :account_size,
                :symbol, :direction, :volume, :open_price, :close_price, :pnl,
                :balance_after, :equity_after,
                :daily_loss_used, :daily_loss_limit,
                :overall_loss_used, :overall_loss_limit, :closed_at, :source,
                :connector_type, :open_time, :fees, CAST(:tags AS jsonb), CAST(:source_metadata AS jsonb), CAST(:import_provenance AS jsonb)
            )
            ON CONFLICT (account_id, symbol, direction, closed_at, pnl) DO UPDATE SET
                connector_type = COALESCE(EXCLUDED.connector_type, trades.connector_type),
                open_time = COALESCE(EXCLUDED.open_time, trades.open_time),
                fees = COALESCE(EXCLUDED.fees, trades.fees),
                tags = COALESCE(EXCLUDED.tags, trades.tags),
                source_metadata = trades.source_metadata || EXCLUDED.source_metadata,
                import_provenance = trades.import_provenance || EXCLUDED.import_provenance
        """), {
            "account_id": account["external_account_id"],
            "account_type": payload.get("account_type") or account.get("account_type"),
            "account_size": account_size,
            "symbol": payload.get("symbol"),
            "direction": payload.get("side"),
            "volume": payload.get("size"),
            "open_price": payload.get("entry_price"),
            "close_price": payload.get("exit_price"),
            "pnl": pnl,
            "balance_after": payload.get("balance_after"),
            "equity_after": payload.get("equity_after"),
            "daily_loss_used": payload.get("daily_loss_used"),
            "daily_loss_limit": payload.get("daily_loss_limit"),
            "overall_loss_used": payload.get("overall_loss_used"),
            "overall_loss_limit": payload.get("overall_loss_limit"),
            "closed_at": _parse_dt(payload.get("close_time")),
            "source": payload.get("source") or _normalize_connector(payload.get("connector_type")),
            "connector_type": _normalize_connector(payload.get("connector_type")),
            "open_time": _parse_dt(payload.get("open_time")),
            "fees": payload.get("fees"),
            "tags": json.dumps(payload.get("tags") or []),
            "source_metadata": json.dumps(payload.get("source_metadata") or {}),
            "import_provenance": json.dumps(payload.get("import_provenance") or {}),
        })
    return True


async def ingest_event(payload: dict[str, Any]) -> None:
    account_id = payload.get("external_account_id")
    trading_account_id = None
    if account_id:
        account = await upsert_trading_account(payload)
        trading_account_id = account["id"]
    params = {
        "trading_account_id": trading_account_id,
        "user_id": payload.get("user_id"),
        "connector_type": _normalize_connector(payload.get("connector_type")),
        "event_type": payload.get("event_type"),
        "event_payload": json.dumps(payload.get("event_payload") or {}),
        "event_time": _parse_dt(payload.get("event_time")) or datetime.now(timezone.utc),
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO connector_events (
                trading_account_id, user_id, connector_type, event_type, event_payload, event_time
            ) VALUES (
                :trading_account_id, :user_id, :connector_type, :event_type, CAST(:event_payload AS jsonb), :event_time
            )
        """), params)
