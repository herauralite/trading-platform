from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
import secrets
import uuid
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import text

from app.core.database import engine
PAIRING_TOKEN_TTL_MINUTES = 15


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


async def check_mt5_pairing_state(
    *,
    user_id: str | None = None,
    external_account_id: str | None = None,
    bridge_url: str | None = None,
    mt5_server: str | None = None,
    bridge_id: str | None = None,
    pairing_token: str | None = None,
) -> dict[str, Any]:
    account_id = str(external_account_id or "").strip()
    server = str(mt5_server or "").strip()
    resolved_bridge_url = _sanitize_bridge_url(bridge_url)
    resolved_bridge_id = str(bridge_id or "").strip()
    resolved_pairing_token = str(pairing_token or "").strip()

    registration_status = await get_user_bridge_registration_state(user_id) if user_id else None
    has_registered_bridge = bool((registration_status or {}).get("active_bridge_count", 0))
    has_pending_pairing = bool((registration_status or {}).get("pending_pairing_token"))

    bridge_status = "bridge_required"
    if has_registered_bridge:
        bridge_status = "bridge_registered"
    elif has_pending_pairing or resolved_bridge_id or resolved_pairing_token:
        bridge_status = "waiting_for_bridge_registration"
    elif resolved_bridge_url:
        bridge_status = "pairing_token_required"

    discovery_status = "account_id_provided" if account_id else "bridge_required"
    registration = {
        "bridge_url_provided": bool(resolved_bridge_url),
        "bridge_url_format_valid": bool(resolved_bridge_url),
        "mt5_server_provided": bool(server),
        "bridge_id_provided": bool(resolved_bridge_id),
        "pairing_token_provided": bool(resolved_pairing_token),
        "trusted_bridge_registered": has_registered_bridge,
        "pairing_token_pending": has_pending_pairing,
    }

    return {
        "bridge_status": bridge_status,
        "discovery_status": discovery_status,
        "pairing_state": _derive_pairing_state(
            bridge_status=bridge_status,
            has_pending_pairing=has_pending_pairing,
            has_registered_bridge=has_registered_bridge,
        ),
        "implementation_mode": "safe_non_probing_pairing",
        "message": "Bridge connectivity is not probed from user-supplied URLs. Pairing remains token-based until a trusted worker is registered.",
        "can_add_account": bool(account_id),
        "discovered_accounts": [],
        "registration": registration,
        "trusted_registration": registration_status or {
            "active_bridge_count": 0,
            "bridges": [],
            "pending_pairing_token": None,
        },
    }


def _derive_pairing_state(*, bridge_status: str, has_pending_pairing: bool, has_registered_bridge: bool) -> str:
    if has_registered_bridge or bridge_status == "bridge_registered":
        return "bridge_registered"
    if has_pending_pairing:
        return "waiting_for_bridge_registration"
    if bridge_status in {"waiting_for_bridge_registration", "pairing_token_required"}:
        return "pairing_token_created"
    return "no_registered_bridge"


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def create_mt5_pairing_token(
    *,
    user_id: str,
    external_account_id: str | None = None,
    mt5_server: str | None = None,
    bridge_url: str | None = None,
    display_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=PAIRING_TOKEN_TTL_MINUTES)
    raw_token = f"mtpair_{secrets.token_urlsafe(24)}"
    token_hash = _hash_token(raw_token)
    token_hint = f"{raw_token[:8]}…{raw_token[-6:]}"
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO mt5_pairing_tokens (
                user_id, token_hash, token_hint, status,
                requested_external_account_id, requested_mt5_server,
                requested_bridge_url, requested_display_name,
                expires_at, metadata
            )
            VALUES (
                :user_id, :token_hash, :token_hint, 'pending',
                :external_account_id, :mt5_server,
                :bridge_url, :display_name,
                :expires_at, CAST(:metadata AS jsonb)
            )
            RETURNING *
        """), {
            "user_id": user_id,
            "token_hash": token_hash,
            "token_hint": token_hint,
            "external_account_id": (external_account_id or "").strip() or None,
            "mt5_server": (mt5_server or "").strip() or None,
            "bridge_url": _sanitize_bridge_url(bridge_url) or None,
            "display_name": (display_name or "").strip() or None,
            "expires_at": expires_at,
            "metadata": json.dumps(metadata or {}),
        })).mappings().first()
    return {
        "pairing_token": raw_token,
        "pairing_token_hint": token_hint,
        "expires_at": row["expires_at"],
        "status": row["status"],
    }


async def register_mt5_trusted_bridge(
    *,
    pairing_token: str,
    machine_label: str | None = None,
    display_name: str | None = None,
    bridge_metadata: dict[str, Any] | None = None,
    remote_ip: str | None = None,
) -> dict[str, Any]:
    normalized_pairing_token = str(pairing_token or "").strip()
    if not normalized_pairing_token:
        raise ValueError("pairing_token_required")
    token_hash = _hash_token(normalized_pairing_token)
    now = datetime.now(timezone.utc)
    bridge_id = f"bridge_{uuid.uuid4().hex[:16]}"
    bridge_secret = f"bridgesecret_{secrets.token_urlsafe(24)}"
    bridge_secret_hash = _hash_token(bridge_secret)
    async with engine.begin() as conn:
        token_row = (await conn.execute(text("""
            SELECT *
            FROM mt5_pairing_tokens
            WHERE token_hash = :token_hash
            LIMIT 1
        """), {"token_hash": token_hash})).mappings().first()
        if token_row is None:
            raise ValueError("pairing_token_invalid")
        if token_row["status"] != "pending":
            raise ValueError("pairing_token_already_used")
        if token_row["expires_at"] is None or token_row["expires_at"] < now:
            await conn.execute(text("""
                UPDATE mt5_pairing_tokens
                SET status = 'expired', updated_at = NOW()
                WHERE id = :id
            """), {"id": token_row["id"]})
            raise ValueError("pairing_token_expired")

        chosen_display_name = str(display_name or token_row.get("requested_display_name") or "").strip() or None
        row = (await conn.execute(text("""
            INSERT INTO mt5_trusted_bridges (
                bridge_id, user_id, pairing_token_id, display_name, machine_label,
                bridge_secret_hash, status, last_heartbeat_at, last_seen_ip, metadata
            )
            VALUES (
                :bridge_id, :user_id, :pairing_token_id, :display_name, :machine_label,
                :bridge_secret_hash, 'registered', NOW(), :last_seen_ip, CAST(:metadata AS jsonb)
            )
            RETURNING *
        """), {
            "bridge_id": bridge_id,
            "user_id": token_row["user_id"],
            "pairing_token_id": token_row["id"],
            "display_name": chosen_display_name,
            "machine_label": (machine_label or "").strip() or None,
            "bridge_secret_hash": bridge_secret_hash,
            "last_seen_ip": (remote_ip or "").strip() or None,
            "metadata": json.dumps(bridge_metadata or {}),
        })).mappings().first()
        await conn.execute(text("""
            UPDATE mt5_pairing_tokens
            SET status = 'consumed',
                consumed_at = NOW(),
                trusted_bridge_id = :bridge_id,
                updated_at = NOW()
            WHERE id = :id
        """), {"id": token_row["id"], "bridge_id": bridge_id})
    return {
        "bridge_id": row["bridge_id"],
        "bridge_secret": bridge_secret,
        "status": row["status"],
        "registered_at": row["created_at"],
        "user_id": row["user_id"],
    }


async def heartbeat_mt5_trusted_bridge(
    *,
    bridge_id: str,
    bridge_secret: str,
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
    remote_ip: str | None = None,
) -> dict[str, Any]:
    normalized_bridge_id = str(bridge_id or "").strip()
    normalized_bridge_secret = str(bridge_secret or "").strip()
    provided_secret_hash = _hash_token(normalized_bridge_secret)
    if not normalized_bridge_id or not normalized_bridge_secret:
        raise ValueError("bridge_auth_required")
    safe_status = str(status or "online").strip().lower()
    if safe_status not in {"registered", "online", "degraded", "offline"}:
        safe_status = "online"
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            SELECT *
            FROM mt5_trusted_bridges
            WHERE bridge_id = :bridge_id
            LIMIT 1
        """), {"bridge_id": normalized_bridge_id})).mappings().first()
        if row is None:
            raise ValueError("bridge_not_found")
        if row["bridge_secret_hash"] != provided_secret_hash:
            raise ValueError("bridge_auth_invalid")
        updated = (await conn.execute(text("""
            UPDATE mt5_trusted_bridges
            SET status = :status,
                last_heartbeat_at = NOW(),
                last_seen_ip = COALESCE(:remote_ip, last_seen_ip),
                metadata = metadata || CAST(:metadata AS jsonb),
                updated_at = NOW()
            WHERE bridge_id = :bridge_id
            RETURNING *
        """), {
            "status": safe_status,
            "bridge_id": normalized_bridge_id,
            "remote_ip": (remote_ip or "").strip() or None,
            "metadata": json.dumps(metadata or {}),
        })).mappings().first()
    return {
        "bridge_id": updated["bridge_id"],
        "status": updated["status"],
        "last_heartbeat_at": updated["last_heartbeat_at"],
    }


async def get_user_bridge_registration_state(user_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        bridges = (await conn.execute(text("""
            SELECT
                bridge_id, display_name, machine_label, status,
                last_heartbeat_at, last_seen_ip, created_at, updated_at
            FROM mt5_trusted_bridges
            WHERE user_id = :user_id
            ORDER BY updated_at DESC
        """), {"user_id": user_id})).mappings().all()
        pending = (await conn.execute(text("""
            SELECT token_hint, status, expires_at, created_at, requested_external_account_id, requested_mt5_server
            FROM mt5_pairing_tokens
            WHERE user_id = :user_id AND status = 'pending' AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
        """), {"user_id": user_id})).mappings().first()
    return {
        "active_bridge_count": len(bridges),
        "bridges": [dict(b) for b in bridges],
        "pending_pairing_token": dict(pending) if pending else None,
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
