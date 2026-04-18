from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
from typing import Any, Protocol

from sqlalchemy import text

from app.core.database import engine


class MT5BridgeClient(Protocol):
    async def get_account_summary(self, account_ref: str) -> dict[str, Any]: ...

    async def get_balances_equity(self, account_ref: str) -> dict[str, Any]: ...

    async def get_open_positions(self, account_ref: str) -> list[dict[str, Any]]: ...

    async def get_orders(self, account_ref: str) -> list[dict[str, Any]]: ...

    async def get_trade_history(self, account_ref: str, *, limit: int = 100) -> list[dict[str, Any]]: ...


@dataclass
class StubMT5BridgeClient:
    """Phase-1 safe bridge stub.

    This contract keeps MT5 behind a bridge interface without claiming live execution.
    """

    def _base(self, account_ref: str) -> dict[str, Any]:
        return {
            "account_ref": account_ref,
            "bridge_status": "bridge_required",
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    async def get_account_summary(self, account_ref: str) -> dict[str, Any]:
        return {
            **self._base(account_ref),
            "summary": {
                "mode": "stub",
                "message": "MT5 bridge worker is not connected yet.",
            },
        }

    async def get_balances_equity(self, account_ref: str) -> dict[str, Any]:
        return {
            **self._base(account_ref),
            "balance": None,
            "equity": None,
            "currency": None,
        }

    async def get_open_positions(self, account_ref: str) -> list[dict[str, Any]]:
        _ = account_ref
        return []

    async def get_orders(self, account_ref: str) -> list[dict[str, Any]]:
        _ = account_ref
        return []

    async def get_trade_history(self, account_ref: str, *, limit: int = 100) -> list[dict[str, Any]]:
        _ = account_ref
        _ = limit
        return []


def build_mt5_bridge_client() -> MT5BridgeClient:
    return StubMT5BridgeClient()


async def upsert_mt5_bridge_account(
    *,
    user_id: str,
    trading_account_id: int,
    external_account_id: str,
    bridge_url: str | None = None,
    mt5_server: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO mt5_bridge_accounts (
                user_id, trading_account_id, external_account_id, bridge_status,
                bridge_url, mt5_server, metadata
            ) VALUES (
                :user_id, :trading_account_id, :external_account_id, 'bridge_required',
                :bridge_url, :mt5_server, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (user_id, trading_account_id)
            DO UPDATE SET
                external_account_id = EXCLUDED.external_account_id,
                bridge_url = COALESCE(EXCLUDED.bridge_url, mt5_bridge_accounts.bridge_url),
                mt5_server = COALESCE(EXCLUDED.mt5_server, mt5_bridge_accounts.mt5_server),
                metadata = mt5_bridge_accounts.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING *
        """), {
            "user_id": user_id,
            "trading_account_id": trading_account_id,
            "external_account_id": external_account_id,
            "bridge_url": bridge_url,
            "mt5_server": mt5_server,
            "metadata": json.dumps(metadata or {}),
        })).mappings().first()
    return dict(row)


async def get_mt5_bridge_account_state(*, user_id: str, trading_account_id: int, external_account_id: str) -> dict[str, Any]:
    client = build_mt5_bridge_client()
    summary, balances, positions, orders, history = await asyncio.gather(
        client.get_account_summary(external_account_id),
        client.get_balances_equity(external_account_id),
        client.get_open_positions(external_account_id),
        client.get_orders(external_account_id),
        client.get_trade_history(external_account_id, limit=50),
    )
    async with engine.connect() as conn:
        bridge_row = (await conn.execute(text("""
            SELECT *
            FROM mt5_bridge_accounts
            WHERE user_id = :user_id AND trading_account_id = :trading_account_id
            LIMIT 1
        """), {
            "user_id": user_id,
            "trading_account_id": trading_account_id,
        })).mappings().first()
    return {
        "connector_type": "mt5_bridge",
        "user_id": user_id,
        "trading_account_id": trading_account_id,
        "external_account_id": external_account_id,
        "bridge_account": dict(bridge_row) if bridge_row else None,
        "summary": summary,
        "balances_equity": balances,
        "open_positions": positions,
        "orders": orders,
        "trade_history": history,
    }
