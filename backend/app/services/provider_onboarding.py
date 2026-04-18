from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import text

from app.core.database import engine
from app.services.alpaca_provider import validate_alpaca_credentials
from app.services.connector_ingest import upsert_connector_config, upsert_connector_lifecycle, upsert_trading_account
from app.services.tradingview_events import normalize_tradingview_event

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
    raw_token = str(token or "").strip()
    if not raw_token:
        raise ValueError("invalid_token")
    token_hash = _token_hash(raw_token)

    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            SELECT tvc.id,
                   tvc.user_id,
                   tvc.trading_account_id,
                   tvc.activation_state,
                   ta.account_key
            FROM tradingview_webhook_connections
            JOIN trading_accounts ta ON ta.id = tvc.trading_account_id
            WHERE tvc.webhook_token_hash = :token_hash
            LIMIT 1
        """), {"token_hash": token_hash})).mappings().first()
        if row is None:
            raise ValueError("not_found")

        normalized_event = normalize_tradingview_event(
            user_id=row["user_id"],
            trading_account_id=row["trading_account_id"],
            account_key=row.get("account_key"),
            connection_id=row["id"],
            payload=payload,
            received_at=now,
        )

        await conn.execute(text("""
            UPDATE tradingview_webhook_connections
            SET activation_state = 'active',
                last_event_at = :now,
                last_event_payload = CAST(:last_event_payload AS jsonb),
                updated_at = :now,
                metadata = metadata
                  || '{"onboarding_state":"active"}'::jsonb
                  || jsonb_build_object('last_event_type', :event_type)
            WHERE id = :id
        """), {
            "id": row["id"],
            "now": now,
            "event_type": normalized_event["event_type"],
            "last_event_payload": json.dumps({
                "event_type": normalized_event["event_type"],
                "symbol": normalized_event["symbol"],
                "timeframe": normalized_event["timeframe"],
                "title": normalized_event["title"],
                "message": normalized_event["message"],
                "received_at": normalized_event["received_at"].isoformat(),
            }),
        })

        await conn.execute(text("""
            UPDATE trading_accounts
            SET metadata = metadata
                || '{"provider_state":"active","onboarding_state":"active"}'::jsonb
                || jsonb_build_object('last_event_at', :now),
                updated_at = :now
            WHERE id = :account_id
        """), {"account_id": row["trading_account_id"], "now": now})

        await conn.execute(text("""
            INSERT INTO connector_events (
                trading_account_id, user_id, connector_type, event_type, event_payload, event_time
            ) VALUES (
                :trading_account_id, :user_id, :connector_type, :event_type, CAST(:event_payload AS jsonb), :event_time
            )
        """), {
            "trading_account_id": normalized_event["trading_account_id"],
            "user_id": normalized_event["user_id"],
            "connector_type": normalized_event["connector_type"],
            "event_type": normalized_event["event_type"],
            "event_payload": json.dumps({
                "external_connection_id": normalized_event["external_connection_id"],
                "account_key": normalized_event["account_key"],
                "symbol": normalized_event["symbol"],
                "timeframe": normalized_event["timeframe"],
                "title": normalized_event["title"],
                "message": normalized_event["message"],
                "raw_payload_json": normalized_event["raw_payload_json"],
                "validity_status": "valid",
                "received_at": normalized_event["received_at"].isoformat(),
            }),
            "event_time": normalized_event["received_at"],
        })

    await upsert_connector_lifecycle(
        user_id=row["user_id"],
        connector_type=TRADINGVIEW_CONNECTOR,
        status="active",
        is_connected=True,
        last_activity_at=now,
        metadata={"provider_state": "active", "onboarding_state": "active"},
    )

    return {
        "ok": True,
        "connection_id": row["id"],
        "state": "active",
        "event_at": now.isoformat(),
        "event_type": normalized_event["event_type"],
    }


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


async def upsert_public_api_connection(
    *,
    user_id: str,
    connector_type: str,
    trading_account_id: int,
    display_label: str,
    environment: str,
    account_alias: str | None,
    provider_state: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload_metadata = metadata or {}
    async with engine.begin() as conn:
        existing = (await conn.execute(text("""
            SELECT id
            FROM public_api_beta_connections
            WHERE user_id = :user_id
              AND connector_type = :connector_type
              AND trading_account_id = :trading_account_id
            ORDER BY updated_at DESC
            LIMIT 1
        """), {
            "user_id": user_id,
            "connector_type": connector_type,
            "trading_account_id": trading_account_id,
        })).mappings().first()
        if existing:
            row = (await conn.execute(text("""
                UPDATE public_api_beta_connections
                SET display_label = :display_label,
                    environment = :environment,
                    account_alias = :account_alias,
                    beta_state = :beta_state,
                    metadata = metadata || CAST(:metadata AS jsonb),
                    updated_at = :updated_at
                WHERE id = :id
                RETURNING *
            """), {
                "id": existing["id"],
                "display_label": display_label,
                "environment": environment,
                "account_alias": account_alias,
                "beta_state": provider_state,
                "metadata": json.dumps(payload_metadata),
                "updated_at": now,
            })).mappings().first()
        else:
            row = (await conn.execute(text("""
                INSERT INTO public_api_beta_connections (
                    user_id, connector_type, trading_account_id, display_label,
                    environment, account_alias, beta_state, metadata, created_at, updated_at
                ) VALUES (
                    :user_id, :connector_type, :trading_account_id, :display_label,
                    :environment, :account_alias, :beta_state, CAST(:metadata AS jsonb), :created_at, :created_at
                )
                RETURNING *
            """), {
                "user_id": user_id,
                "connector_type": connector_type,
                "trading_account_id": trading_account_id,
                "display_label": display_label,
                "environment": environment,
                "account_alias": account_alias,
                "beta_state": provider_state,
                "metadata": json.dumps(payload_metadata),
                "created_at": now,
            })).mappings().first()
    return dict(row)


async def connect_alpaca_api(
    *,
    user_id: str,
    display_label: str,
    api_key: str,
    api_secret: str,
    environment: str | None = None,
    account_alias: str | None = None,
) -> dict[str, Any]:
    validated_at = datetime.now(timezone.utc)
    summary = await validate_alpaca_credentials(
        api_key=api_key,
        api_secret=api_secret,
        environment=environment,
    )
    env = str(summary["environment"])
    provider_state = "paper_connected" if env == "paper" else "live_connected"
    account = await upsert_trading_account({
        "user_id": user_id,
        "connector_type": "alpaca_api",
        "broker_name": "alpaca",
        "external_account_id": summary["account_id"],
        "display_label": display_label,
        "metadata": {
            "provider_state": provider_state,
            "account_verified": True,
            "environment": env,
            "account_status": summary.get("account_status"),
            "last_validated_at": validated_at.isoformat(),
            "account_alias": (account_alias or "").strip() or None,
            "currency": summary.get("currency"),
        },
    })
    await upsert_connector_config(
        user_id,
        "alpaca_api",
        non_secret_config={"environment": env},
        secret_config={"api_key": api_key, "api_secret": api_secret},
        status="configured",
        validation_error=None,
    )
    connection = await upsert_public_api_connection(
        user_id=user_id,
        connector_type="alpaca_api",
        trading_account_id=account["id"],
        display_label=display_label,
        environment=env,
        account_alias=account_alias,
        provider_state=provider_state,
        metadata={
            "account_verified": True,
            "alpaca_account_id": summary["account_id"],
            "last_validated_at": validated_at.isoformat(),
            "account_status": summary.get("account_status"),
        },
    )
    lifecycle = await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type="alpaca_api",
        status=provider_state,
        is_connected=True,
        last_sync_at=validated_at,
        last_activity_at=validated_at,
        metadata={
            "provider_state": provider_state,
            "account_verified": True,
            "environment": env,
            "last_validated_at": validated_at.isoformat(),
        },
    )
    return {
        "provider": "alpaca_api",
        "provider_state": provider_state,
        "account_verified": True,
        "environment": env,
        "validated_at": validated_at,
        "account_summary": {
            "account_id": summary["account_id"],
            "account_number": summary.get("account_number"),
            "status": summary.get("account_status"),
            "currency": summary.get("currency"),
            "equity": summary.get("equity"),
            "buying_power": summary.get("buying_power"),
        },
        "trading_account": account,
        "connection": connection,
        "lifecycle": lifecycle,
    }
