from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
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


def _sanitize_bridge_url(bridge_url: str | None) -> str:
    candidate = str(bridge_url or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    return candidate.rstrip("/")


def _normalize_discovered_account(raw: dict[str, Any]) -> dict[str, Any]:
    external_account_id = str(raw.get("external_account_id") or raw.get("account_id") or "").strip()
    return {
        "external_account_id": external_account_id,
        "display_label": str(raw.get("display_label") or raw.get("label") or external_account_id).strip() or external_account_id,
        "mt5_server": str(raw.get("mt5_server") or raw.get("server") or "").strip() or None,
    }


async def check_mt5_pairing_state(
    *,
    external_account_id: str | None = None,
    bridge_url: str | None = None,
    mt5_server: str | None = None,
) -> dict[str, Any]:
    account_id = str(external_account_id or "").strip()
    server = str(mt5_server or "").strip()
    resolved_bridge_url = _sanitize_bridge_url(bridge_url)
    if not resolved_bridge_url:
        return {
            "bridge_status": "bridge_required",
            "discovery_status": "bridge_required",
            "implementation_mode": "stub_safe",
            "message": "Bridge URL is required before discovery can run.",
            "can_add_account": bool(account_id),
            "discovered_accounts": [],
        }

    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0)) as client:
        health_url = f"{resolved_bridge_url}/health"
        try:
            health_res = await client.get(health_url)
            health_ok = health_res.status_code < 400
        except httpx.HTTPError:
            health_ok = False

        if not health_ok:
            return {
                "bridge_status": "bridge_not_reachable",
                "discovery_status": "bridge_not_reachable",
                "implementation_mode": "stub_safe",
                "message": "Bridge endpoint is not reachable from the API host.",
                "can_add_account": bool(account_id),
                "discovered_accounts": [],
            }

        discovery_url = f"{resolved_bridge_url}/accounts/discover"
        try:
            discovery_res = await client.post(discovery_url, json={
                "external_account_id": account_id or None,
                "mt5_server": server or None,
            })
        except httpx.HTTPError:
            return {
                "bridge_status": "waiting_for_bridge",
                "discovery_status": "waiting_for_bridge",
                "implementation_mode": "stub_safe",
                "message": "Bridge health responded, but discovery endpoint is not ready.",
                "can_add_account": bool(account_id),
                "discovered_accounts": [],
            }

    if discovery_res.status_code >= 400:
        return {
            "bridge_status": "waiting_for_bridge",
            "discovery_status": "waiting_for_bridge",
            "implementation_mode": "stub_safe",
            "message": "Bridge health is reachable, but discovery endpoint returned an error.",
            "can_add_account": bool(account_id),
            "discovered_accounts": [],
        }

    payload = discovery_res.json() if discovery_res.headers.get("content-type", "").lower().find("json") >= 0 else {}
    discovered = payload.get("discovered_accounts") or payload.get("accounts") or []
    normalized_accounts = [
        _normalize_discovered_account(item)
        for item in discovered
        if isinstance(item, dict)
    ]
    normalized_accounts = [item for item in normalized_accounts if item["external_account_id"]]

    if account_id and any(item["external_account_id"] == account_id for item in normalized_accounts):
        discovery_status = "discovered_account_ready"
    elif normalized_accounts:
        discovery_status = "discovered_accounts_available"
    else:
        discovery_status = "account_not_discovered_yet"

    return {
        "bridge_status": "bridge_reachable",
        "discovery_status": discovery_status,
        "implementation_mode": "bridge_discovery_probe",
        "message": "Bridge probe completed.",
        "can_add_account": bool(account_id),
        "discovered_accounts": normalized_accounts,
    }


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
