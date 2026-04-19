from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import text

from app.core.database import engine
from app.services.alpaca_provider import (
    AlpacaCredentialValidationError,
    normalize_alpaca_environment,
    validate_alpaca_credentials,
)
from app.services.connector_ingest import upsert_connector_config, upsert_connector_lifecycle, upsert_trading_account
from app.services.secret_crypto import encrypt_secret
from app.services.tradelocker_provider import TradeLockerAuthError, TradeLockerClient
from app.services.tradingview_events import normalize_tradingview_event

TRADINGVIEW_CONNECTOR = "tradingview_webhook"
PUBLIC_API_BETA_CONNECTORS = {"oanda_api", "binance_api"}


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


async def connect_alpaca_api_account(
    *,
    user_id: str,
    label: str,
    environment: str | None,
    api_key: str,
    api_secret: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    env = normalize_alpaca_environment(environment)
    display_label = str(label or "").strip() or "Alpaca API"
    normalized_key = str(api_key or "").strip()
    normalized_secret = str(api_secret or "").strip()
    if not normalized_key or not normalized_secret:
        raise ValueError("invalid_credentials")

    try:
        account_summary = await validate_alpaca_credentials(
            environment=env,
            api_key=normalized_key,
            api_secret=normalized_secret,
        )
    except AlpacaCredentialValidationError as exc:
        validation_error = str(exc)
        await upsert_connector_lifecycle(
            user_id=user_id,
            connector_type="alpaca_api",
            status="validation_failed",
            is_connected=False,
            last_activity_at=now,
            metadata={
                "provider_state": "validation_failed",
                "environment": env,
                "validation_error": validation_error,
            },
            error=validation_error,
        )
        raise

    provider_state = str(account_summary["provider_state"])
    validation_error: str | None = None
    validation_state = str(account_summary.get("validation_state") or "account_verified")

    account = await upsert_trading_account({
        "user_id": user_id,
        "connector_type": "alpaca_api",
        "broker_name": "alpaca",
        "external_account_id": account_summary["alpaca_account_number"],
        "display_label": display_label,
        "metadata": {
            "provider_state": provider_state,
            "validation_state": validation_state,
            "onboarding_state": "credentials_validated",
            "environment": env,
            "alpaca_status": account_summary.get("alpaca_status"),
            "account_summary": account_summary,
            "last_validated_at": now.isoformat(),
        },
    })

    encrypted_api_key = encrypt_secret(normalized_key)
    encrypted_api_secret = encrypt_secret(normalized_secret)
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO public_api_beta_connections (
                user_id, connector_type, trading_account_id, display_label,
                environment, beta_state, encrypted_api_key, encrypted_api_secret,
                account_summary, last_validation_error, last_validated_at, metadata, created_at, updated_at
            ) VALUES (
                :user_id, 'alpaca_api', :trading_account_id, :display_label,
                :environment, :beta_state, :encrypted_api_key, :encrypted_api_secret,
                CAST(:account_summary AS jsonb), :last_validation_error, :last_validated_at, CAST(:metadata AS jsonb), :created_at, :created_at
            )
            ON CONFLICT (trading_account_id) DO UPDATE SET
                display_label = EXCLUDED.display_label,
                environment = EXCLUDED.environment,
                beta_state = EXCLUDED.beta_state,
                encrypted_api_key = EXCLUDED.encrypted_api_key,
                encrypted_api_secret = EXCLUDED.encrypted_api_secret,
                account_summary = EXCLUDED.account_summary,
                last_validation_error = EXCLUDED.last_validation_error,
                last_validated_at = EXCLUDED.last_validated_at,
                metadata = public_api_beta_connections.metadata || EXCLUDED.metadata,
                updated_at = :created_at
            RETURNING id, user_id, connector_type, trading_account_id, display_label,
                      environment, beta_state, account_summary, last_validation_error, last_validated_at, created_at, updated_at
        """), {
            "user_id": user_id,
            "trading_account_id": account["id"],
            "display_label": display_label,
            "environment": env,
            "beta_state": provider_state,
            "encrypted_api_key": encrypted_api_key,
            "encrypted_api_secret": encrypted_api_secret,
            "account_summary": json.dumps(account_summary),
            "last_validation_error": validation_error,
            "last_validated_at": now,
            "metadata": json.dumps({
                "provider_state": provider_state,
                "environment": env,
                "validation_state": validation_state,
                "onboarding_state": "credentials_validated",
            }),
            "created_at": now,
        })).mappings().first()

    await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type="alpaca_api",
        status=provider_state,
        is_connected=provider_state in {"paper_connected", "live_connected"},
        last_activity_at=now,
        metadata={
            "provider_state": provider_state,
            "environment": env,
            "validation_state": validation_state,
            "validation_error": validation_error,
        },
        error=validation_error,
    )
    return {
        "provider_state": provider_state,
        "environment": env,
        "validation_error": validation_error,
        "account": dict(row),
    }


async def connect_tradelocker_api_account(
    *,
    user_id: str,
    label: str,
    base_url: str,
    account_id: str,
    email: str,
    password: str,
    server: str | None = None,
    environment: str | None = "demo",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    normalized_account_id = str(account_id or "").strip()
    normalized_email = str(email or "").strip()
    normalized_password = str(password or "").strip()
    display_label = str(label or "").strip() or "TradeLocker API"
    normalized_env = str(environment or "demo").strip().lower()
    if not normalized_base_url or not normalized_account_id:
        raise ValueError("missing_account_context")
    if not normalized_email or not normalized_password:
        raise ValueError("invalid_credentials")

    client = TradeLockerClient(base_url=normalized_base_url)
    try:
        session = await client.login_password(
            email=normalized_email,
            password=normalized_password,
            server=server,
        )
        account_payload = await client.get_account(session["access_token"], normalized_account_id)
    except TradeLockerAuthError as exc:
        await upsert_connector_lifecycle(
            user_id=user_id,
            connector_type="tradelocker_api",
            status="validation_failed",
            is_connected=False,
            last_activity_at=now,
            metadata={
                "provider_state": "validation_failed",
                "validation_error": str(exc),
                "environment": normalized_env,
            },
            error=str(exc),
        )
        raise

    external_account_id = str(
        account_payload.get("id")
        or account_payload.get("accountId")
        or account_payload.get("account_id")
        or normalized_account_id
    ).strip()
    account = await upsert_trading_account({
        "user_id": user_id,
        "connector_type": "tradelocker_api",
        "broker_name": "tradelocker",
        "external_account_id": external_account_id,
        "display_label": display_label,
        "metadata": {
            "provider_state": "connected",
            "validation_state": "account_verified",
            "onboarding_state": "credentials_validated",
            "environment": normalized_env,
            "last_validated_at": now.isoformat(),
        },
    })

    await upsert_connector_config(
        user_id=user_id,
        connector_type="tradelocker_api",
        status="configured",
        validation_error=None,
        non_secret_config={
            "base_url": normalized_base_url,
            "account_id": external_account_id,
            "environment": normalized_env,
            "access_token_expires_at": session["expires_at"].isoformat(),
            "last_synced_at": None,
        },
        secret_config={
            "encrypted_access_token": encrypt_secret(session["access_token"]),
            "encrypted_refresh_token": encrypt_secret(session["refresh_token"]),
            "encrypted_email": encrypt_secret(normalized_email),
            "encrypted_password": encrypt_secret(normalized_password),
            "server": str(server or "").strip(),
        },
    )

    await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type="tradelocker_api",
        status="connected",
        is_connected=True,
        last_activity_at=now,
        metadata={
            "provider_state": "connected",
            "validation_state": "account_verified",
            "environment": normalized_env,
        },
    )

    return {
        "provider_state": "connected",
        "environment": normalized_env,
        "validation_error": None,
        "account": {
            "id": account["id"],
            "display_label": display_label,
            "external_account_id": external_account_id,
            "last_validated_at": now,
        },
    }
