from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import text

from app.core.database import engine
from app.services.connector_ingest import upsert_connector_lifecycle, upsert_trading_account

TRADINGVIEW_CONNECTOR = "tradingview_webhook"
PUBLIC_API_BETA_CONNECTORS = {"alpaca_api", "oanda_api", "binance_api"}


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _token_hint(raw: str) -> str:
    return f"{raw[:8]}…{raw[-6:]}" if len(raw) > 16 else raw


async def create_tradingview_connection(*, user_id: str, display_label: str, account_alias: str | None = None) -> dict[str, Any]:
    raw_token = f"tvw_{secrets.token_urlsafe(24)}"
    token_hash = _token_hash(raw_token)
    token_hint = _token_hint(raw_token)
    now = datetime.now(timezone.utc)

    account = await upsert_trading_account({
        "user_id": user_id,
        "connector_type": TRADINGVIEW_CONNECTOR,
        "broker_name": "tradingview",
        "external_account_id": f"tv-{token_hint.replace('…', '')}",
        "display_label": display_label,
        "metadata": {
            "provider_state": "awaiting_alerts",
            "onboarding_state": "webhook_created",
            "account_alias": (account_alias or "").strip() or None,
        },
    })

    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO tradingview_webhook_connections (
                user_id, trading_account_id, display_label, account_alias,
                webhook_token_hash, webhook_token_hint, activation_state, metadata, created_at, updated_at
            ) VALUES (
                :user_id, :trading_account_id, :display_label, :account_alias,
                :webhook_token_hash, :webhook_token_hint, 'awaiting_alerts', CAST(:metadata AS jsonb), :created_at, :created_at
            )
            RETURNING id, user_id, trading_account_id, display_label, account_alias,
                      webhook_token_hint, activation_state, last_event_at, created_at
        """), {
            "user_id": user_id,
            "trading_account_id": account["id"],
            "display_label": display_label,
            "account_alias": (account_alias or "").strip() or None,
            "webhook_token_hash": token_hash,
            "webhook_token_hint": token_hint,
            "metadata": json.dumps({"onboarding_state": "webhook_created"}),
            "created_at": now,
        })).mappings().first()

    await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type=TRADINGVIEW_CONNECTOR,
        status="awaiting_alerts",
        is_connected=False,
        last_activity_at=now,
        metadata={"provider_state": "awaiting_alerts", "onboarding_state": "webhook_created"},
    )

    return {
        **dict(row),
        "webhook_token": raw_token,
    }


async def ingest_tradingview_event(*, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    token_hash = _token_hash(str(token or "").strip())
    if not token_hash:
        raise ValueError("invalid_token")

    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            SELECT *
            FROM tradingview_webhook_connections
            WHERE webhook_token_hash = :token_hash
            LIMIT 1
        """), {"token_hash": token_hash})).mappings().first()
        if row is None:
            raise ValueError("not_found")

        await conn.execute(text("""
            UPDATE tradingview_webhook_connections
            SET activation_state = 'active',
                last_event_at = :now,
                last_event_payload = CAST(:payload AS jsonb),
                updated_at = :now,
                metadata = metadata || '{"onboarding_state":"active"}'::jsonb
            WHERE id = :id
        """), {"id": row["id"], "now": now, "payload": json.dumps(payload or {})})

        await conn.execute(text("""
            UPDATE trading_accounts
            SET metadata = metadata || '{"provider_state":"active"}'::jsonb,
                updated_at = :now
            WHERE id = :account_id
        """), {"account_id": row["trading_account_id"], "now": now})

    await upsert_connector_lifecycle(
        user_id=row["user_id"],
        connector_type=TRADINGVIEW_CONNECTOR,
        status="active",
        is_connected=True,
        last_activity_at=now,
        metadata={"provider_state": "active", "onboarding_state": "active"},
    )

    return {"ok": True, "connection_id": row["id"], "state": "active", "event_at": now.isoformat()}


async def create_public_api_beta_connection(
    *,
    user_id: str,
    connector_type: str,
    display_label: str,
    environment: str | None,
    account_alias: str | None,
) -> dict[str, Any]:
    normalized = str(connector_type or "").strip().lower()
    if normalized not in PUBLIC_API_BETA_CONNECTORS:
        raise ValueError("unsupported_provider")
    now = datetime.now(timezone.utc)
    env = str(environment or "paper").strip().lower()
    if env not in {"paper", "live"}:
        env = "paper"

    account = await upsert_trading_account({
        "user_id": user_id,
        "connector_type": normalized,
        "broker_name": normalized,
        "external_account_id": f"beta-{normalized}-{secrets.token_hex(4)}",
        "display_label": display_label,
        "metadata": {
            "provider_state": "awaiting_secure_auth",
            "onboarding_state": "metadata_saved",
            "environment": env,
            "account_alias": (account_alias or "").strip() or None,
        },
    })

    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO public_api_beta_connections (
                user_id, connector_type, trading_account_id, display_label,
                environment, account_alias, beta_state, metadata, created_at, updated_at
            ) VALUES (
                :user_id, :connector_type, :trading_account_id, :display_label,
                :environment, :account_alias, 'awaiting_secure_auth', CAST(:metadata AS jsonb), :created_at, :created_at
            )
            RETURNING id, user_id, connector_type, trading_account_id, display_label,
                      environment, account_alias, beta_state, created_at
        """), {
            "user_id": user_id,
            "connector_type": normalized,
            "trading_account_id": account["id"],
            "display_label": display_label,
            "environment": env,
            "account_alias": (account_alias or "").strip() or None,
            "metadata": json.dumps({"onboarding_state": "metadata_saved"}),
            "created_at": now,
        })).mappings().first()

    await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type=normalized,
        status="awaiting_secure_auth",
        is_connected=False,
        last_activity_at=now,
        metadata={"provider_state": "awaiting_secure_auth", "onboarding_state": "metadata_saved", "environment": env},
    )
    return dict(row)
