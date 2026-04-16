from datetime import datetime, timezone
from typing import Any
import json

from sqlalchemy import text

from app.core.database import engine


def _normalize_connector(value: str | None) -> str:
    return (value or "manual").strip().lower().replace("-", "_")


def _parse_dt(value: Any):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


async def ensure_connector_tables() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trading_accounts (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                connector_type TEXT NOT NULL,
                broker_name TEXT,
                external_account_id TEXT NOT NULL,
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
                source_metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (trading_account_id, symbol, side)
            )
        """))
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


async def upsert_trading_account(account: dict[str, Any]) -> dict[str, Any]:
    connector_type = _normalize_connector(account.get("connector_type") or account.get("source_connector"))
    user_id = account.get("user_id")
    external_account_id = str(account.get("external_account_id") or "").strip()
    if not external_account_id:
        raise ValueError("external_account_id is required")

    params = {
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
                user_id, connector_type, broker_name, external_account_id,
                display_label, account_type, account_size, is_active, metadata
            ) VALUES (
                :user_id, :connector_type, :broker_name, :external_account_id,
                :display_label, :account_type, :account_size, :is_active, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (user_id, connector_type, external_account_id)
            DO UPDATE SET
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


async def ingest_account_snapshot(payload: dict[str, Any]) -> None:
    account = await upsert_trading_account(payload)
    params = {
        "trading_account_id": account["id"],
        "snapshot_time": _parse_dt(payload.get("timestamp")) or datetime.now(timezone.utc),
        "balance": payload.get("balance"),
        "equity": payload.get("equity"),
        "drawdown": payload.get("drawdown"),
        "risk_used": payload.get("risk_used"),
        "source_metadata": json.dumps(payload.get("source_metadata") or {}),
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO account_snapshots (
                trading_account_id, snapshot_time, balance, equity, drawdown, risk_used, source_metadata
            ) VALUES (
                :trading_account_id, :snapshot_time, :balance, :equity, :drawdown, :risk_used, CAST(:source_metadata AS jsonb)
            )
        """), params)


async def ingest_position(payload: dict[str, Any]) -> None:
    account = await upsert_trading_account(payload)
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
        "source_metadata": json.dumps(payload.get("source_metadata") or {}),
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO positions (
                trading_account_id, symbol, side, size, average_entry, unrealized_pnl,
                stop_loss, take_profit, opened_at, source_metadata
            ) VALUES (
                :trading_account_id, :symbol, :side, :size, :average_entry, :unrealized_pnl,
                :stop_loss, :take_profit, :opened_at, CAST(:source_metadata AS jsonb)
            )
            ON CONFLICT (trading_account_id, symbol, side)
            DO UPDATE SET
                size = EXCLUDED.size,
                average_entry = EXCLUDED.average_entry,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                stop_loss = EXCLUDED.stop_loss,
                take_profit = EXCLUDED.take_profit,
                opened_at = COALESCE(EXCLUDED.opened_at, positions.opened_at),
                source_metadata = positions.source_metadata || EXCLUDED.source_metadata,
                updated_at = NOW()
        """), params)


async def ingest_trade(payload: dict[str, Any]) -> bool:
    account = await upsert_trading_account(payload)
    account_size = payload.get("account_size") or account.get("account_size") or 10000
    pnl = payload.get("pnl") or 0
    if abs(pnl) > account_size:
        return False

    params = {
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
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO trades (
                account_id, account_type, account_size,
                symbol, direction, volume, open_price, close_price, pnl,
                balance_after, equity_after,
                daily_loss_used, daily_loss_limit,
                overall_loss_used, overall_loss_limit, closed_at, source
            ) VALUES (
                :account_id, :account_type, :account_size,
                :symbol, :direction, :volume, :open_price, :close_price, :pnl,
                :balance_after, :equity_after,
                :daily_loss_used, :daily_loss_limit,
                :overall_loss_used, :overall_loss_limit, :closed_at, :source
            )
            ON CONFLICT (account_id, symbol, direction, closed_at, pnl) DO NOTHING
        """), params)
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
