import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from app.core.database import engine
from app.core.extension_auth import (
    EXTENSION_SESSION_TTL_SECONDS,
    create_extension_access_token,
    hash_extension_session_secret,
)
from app.services.connector_ingest import (
    deactivate_missing_positions,
    ingest_account_snapshot,
    ingest_position,
    upsert_trading_account,
)
from app.services.execution_status import validate_command_transition

PAIR_TOKEN_TTL_MINUTES = 10
COMMAND_POLL_LIMIT = 50
DISPATCH_LEASE_SECONDS = int(__import__("os").getenv("EXECUTION_DISPATCH_LEASE_SECONDS", "45"))
COMMAND_TERMINAL_STATUSES = {"succeeded", "failed", "expired"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_pair_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _json(value: dict[str, Any] | list[Any] | None) -> str:
    return json.dumps(value or {})


async def start_pairing(user_id: str, device_label: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    pair_code = secrets.token_urlsafe(10)
    pair_secret = secrets.token_urlsafe(32)
    expires_at = _utcnow() + timedelta(minutes=PAIR_TOKEN_TTL_MINUTES)
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
            INSERT INTO extension_pairing_tokens (
                user_id, pair_code, pair_secret_hash, status, expires_at, metadata
            ) VALUES (
                :user_id, :pair_code, :pair_secret_hash, 'pending', :expires_at, CAST(:metadata AS jsonb)
            )
            RETURNING id, pair_code, expires_at
            """
                ),
                {
                    "user_id": user_id,
                    "pair_code": pair_code,
                    "pair_secret_hash": _hash_pair_secret(pair_secret),
                    "expires_at": expires_at,
                    "metadata": _json({"device_label": device_label, **(metadata or {})}),
                },
            )
        ).mappings().first()
    return {"pair_code": row["pair_code"], "pair_secret": pair_secret, "expires_at": row["expires_at"].isoformat()}


async def complete_pairing(pair_code: str, pair_secret: str, device_info: dict[str, Any]) -> dict[str, Any]:
    now = _utcnow()
    extension_session_secret = secrets.token_urlsafe(48)
    extension_session_secret_hash = hash_extension_session_secret(extension_session_secret)
    session_expires_at = now + timedelta(seconds=EXTENSION_SESSION_TTL_SECONDS)

    async with engine.begin() as conn:
        token = (
            await conn.execute(text("SELECT * FROM extension_pairing_tokens WHERE pair_code = :pair_code"), {"pair_code": pair_code})
        ).mappings().first()
        if not token:
            raise HTTPException(status_code=404, detail="Pair token not found")
        if token["status"] != "pending":
            raise HTTPException(status_code=409, detail="Pair token already used")
        if token["expires_at"] < now:
            raise HTTPException(status_code=410, detail="Pair token expired")
        if token["pair_secret_hash"] != _hash_pair_secret(pair_secret):
            raise HTTPException(status_code=401, detail="Invalid pair secret")

        device = (
            await conn.execute(
                text(
                    """
            INSERT INTO extension_devices (
                user_id, device_fingerprint, label, platform, browser, extension_version,
                status, last_seen_at, metadata
            ) VALUES (
                :user_id, :device_fingerprint, :label, :platform, :browser, :extension_version,
                'online', :now, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (user_id, device_fingerprint)
            DO UPDATE SET
                label = COALESCE(EXCLUDED.label, extension_devices.label),
                platform = COALESCE(EXCLUDED.platform, extension_devices.platform),
                browser = COALESCE(EXCLUDED.browser, extension_devices.browser),
                extension_version = COALESCE(EXCLUDED.extension_version, extension_devices.extension_version),
                status = 'online',
                last_seen_at = EXCLUDED.last_seen_at,
                metadata = extension_devices.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING *
            """
                ),
                {
                    "user_id": token["user_id"],
                    "device_fingerprint": device_info.get("device_fingerprint") or "unknown-device",
                    "label": device_info.get("label"),
                    "platform": device_info.get("platform"),
                    "browser": device_info.get("browser"),
                    "extension_version": device_info.get("extension_version"),
                    "now": now,
                    "metadata": _json(device_info.get("metadata") or {}),
                },
            )
        ).mappings().first()

        session = (
            await conn.execute(
                text(
                    """
            INSERT INTO extension_sessions (
                user_id,
                extension_device_id,
                status,
                started_at,
                last_heartbeat_at,
                expires_at,
                session_secret_hash,
                metadata
            ) VALUES (
                :user_id,
                :device_id,
                'active',
                :now,
                :now,
                :expires_at,
                :session_secret_hash,
                CAST(:metadata AS jsonb)
            )
            RETURNING *
            """
                ),
                {
                    "user_id": token["user_id"],
                    "device_id": device["id"],
                    "now": now,
                    "expires_at": session_expires_at,
                    "session_secret_hash": extension_session_secret_hash,
                    "metadata": _json({"paired_from": "extension", "pair_code": pair_code}),
                },
            )
        ).mappings().first()

        await conn.execute(
            text(
                """
            UPDATE extension_pairing_tokens
            SET status = 'completed', consumed_at = :now, metadata = metadata || :metadata::jsonb
            WHERE id = :id
            """
            ),
            {"id": token["id"], "now": now, "metadata": _json({"extension_device_id": device["id"]})},
        )

    access_token = create_extension_access_token(
        extension_session_id=int(session["id"]),
        extension_device_id=int(device["id"]),
        user_id=str(token["user_id"]),
    )

    return {
        "extension_device_id": int(device["id"]),
        "extension_session_id": int(session["id"]),
        "user_id": str(token["user_id"]),
        "extension_access_token": access_token,
        "token_type": "bearer",
        "expires_at": session_expires_at.isoformat(),
    }


async def heartbeat_extension(extension_ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    now = _utcnow()
    async with engine.begin() as conn:
        device = (
            await conn.execute(
                text(
                    """
            UPDATE extension_devices
            SET status = 'online', last_seen_at = :now, metadata = metadata || :metadata::jsonb, updated_at = NOW()
            WHERE id = :device_id AND user_id = :user_id
            RETURNING *
            """
                ),
                {
                    "device_id": extension_ctx["extension_device_id"],
                    "user_id": extension_ctx["user_id"],
                    "now": now,
                    "metadata": _json(payload.get("metadata")),
                },
            )
        ).mappings().first()
        if not device:
            raise HTTPException(status_code=404, detail="Extension device not found")
        await conn.execute(
            text(
                """
            UPDATE extension_sessions
            SET status = 'active',
                last_heartbeat_at = :now,
                metadata = metadata || :metadata::jsonb,
                updated_at = NOW()
            WHERE id = :session_id
              AND extension_device_id = :device_id
              AND user_id = :user_id
              AND status = 'active'
            """
            ),
            {
                "session_id": extension_ctx["extension_session_id"],
                "device_id": extension_ctx["extension_device_id"],
                "user_id": extension_ctx["user_id"],
                "now": now,
                "metadata": _json(payload.get("session_metadata")),
            },
        )
    return {"ok": True, "at": now.isoformat()}


async def upsert_platform_session(extension_ctx: dict[str, Any], sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with engine.begin() as conn:
        for session in sessions:
            row = (
                await conn.execute(
                    text(
                        """
                INSERT INTO platform_sessions (
                    user_id, extension_device_id, adapter_key, platform_key, tab_id, tab_url,
                    platform_account_ref, session_ref, status, capabilities, metadata, last_seen_at
                ) VALUES (
                    :user_id, :device_id, :adapter_key, :platform_key, :tab_id, :tab_url,
                    :platform_account_ref, :session_ref, :status, CAST(:capabilities AS jsonb), CAST(:metadata AS jsonb), NOW()
                )
                ON CONFLICT (extension_device_id, adapter_key, tab_id)
                DO UPDATE SET
                    platform_key = EXCLUDED.platform_key,
                    tab_url = EXCLUDED.tab_url,
                    platform_account_ref = COALESCE(EXCLUDED.platform_account_ref, platform_sessions.platform_account_ref),
                    session_ref = COALESCE(EXCLUDED.session_ref, platform_sessions.session_ref),
                    status = EXCLUDED.status,
                    capabilities = platform_sessions.capabilities || EXCLUDED.capabilities,
                    metadata = platform_sessions.metadata || EXCLUDED.metadata,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                RETURNING *
                """
                    ),
                    {
                        "user_id": extension_ctx["user_id"],
                        "device_id": extension_ctx["extension_device_id"],
                        "adapter_key": session.get("adapter_key"),
                        "platform_key": session.get("platform_key"),
                        "tab_id": str(session.get("tab_id")),
                        "tab_url": session.get("tab_url"),
                        "platform_account_ref": session.get("platform_account_ref"),
                        "session_ref": session.get("session_ref"),
                        "status": session.get("status", "active"),
                        "capabilities": _json(session.get("capabilities") or {}),
                        "metadata": _json(session.get("metadata") or {}),
                    },
                )
            ).mappings().first()
            results.append(dict(row))
    return results


async def _resolve_platform_session_id(
    user_id: str,
    extension_device_id: int,
    adapter_key: str,
    account_payload: dict[str, Any],
) -> int | None:
    platform_session_id = account_payload.get("platform_session_id")
    if platform_session_id:
        return int(platform_session_id)

    tab_id = account_payload.get("tab_id")
    session_ref = account_payload.get("session_ref")
    if tab_id is None and not session_ref:
        return None

    query = """
        SELECT id
        FROM platform_sessions
        WHERE user_id = :user_id
          AND extension_device_id = :extension_device_id
          AND adapter_key = :adapter_key
          AND (
            (:tab_id IS NOT NULL AND tab_id = :tab_id::text)
            OR (:session_ref IS NOT NULL AND session_ref = :session_ref)
          )
        ORDER BY updated_at DESC
        LIMIT 1
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(query),
                {
                    "user_id": user_id,
                    "extension_device_id": extension_device_id,
                    "adapter_key": adapter_key,
                    "tab_id": str(tab_id) if tab_id is not None else None,
                    "session_ref": session_ref,
                },
            )
        ).mappings().first()
    return int(row["id"]) if row else None


async def ingest_state_sync(extension_ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    synced_accounts: list[dict[str, Any]] = []
    for account in payload.get("accounts") or []:
        adapter_key = account.get("adapter_key") or "unknown_adapter"
        platform_session_id = await _resolve_platform_session_id(
            extension_ctx["user_id"],
            extension_ctx["extension_device_id"],
            adapter_key,
            account,
        )

        normalized_account = {
            "user_id": extension_ctx["user_id"],
            "connector_type": account.get("platform_key") or adapter_key,
            "broker_name": account.get("platform_name"),
            "external_account_id": account.get("platform_account_ref"),
            "display_label": account.get("display_label"),
            "account_type": account.get("account_type"),
            "account_size": account.get("account_size"),
            "platform_key": account.get("platform_key"),
            "platform_account_ref": account.get("platform_account_ref"),
            "extension_device_id": extension_ctx["extension_device_id"],
            "platform_session_id": platform_session_id,
            "execution_enabled": True,
            "metadata": {
                "adapter_key": adapter_key,
                "platform_key": account.get("platform_key"),
                "extension_device_id": extension_ctx["extension_device_id"],
                "platform_session_id": platform_session_id,
            },
        }

        trading_account = await upsert_trading_account(normalized_account)
        snapshot = account.get("snapshot") or {}
        await ingest_account_snapshot(
            {
                **normalized_account,
                "timestamp": snapshot.get("timestamp") or _utcnow().isoformat(),
                "balance": snapshot.get("balance"),
                "equity": snapshot.get("equity"),
                "drawdown": snapshot.get("drawdown"),
                "risk_used": snapshot.get("risk_used"),
                "source_metadata": snapshot.get("source_metadata") or {},
            }
        )

        seen_keys: list[str] = []
        for position in account.get("positions") or []:
            position_payload = {
                **normalized_account,
                **position,
                "source_metadata": position.get("source_metadata") or {},
            }
            key = await ingest_position(position_payload)
            seen_keys.append(key)
        await deactivate_missing_positions(trading_account["id"], seen_keys, allow_empty_snapshot=True)

        async with engine.begin() as conn:
            for order in account.get("orders") or []:
                await conn.execute(
                    text(
                        """
                    INSERT INTO canonical_orders (
                        trading_account_id, platform_order_ref, symbol, side, order_type,
                        status, quantity, filled_quantity, price, stop_price, submitted_at,
                        source_metadata, last_seen_at
                    ) VALUES (
                        :trading_account_id, :platform_order_ref, :symbol, :side, :order_type,
                        :status, :quantity, :filled_quantity, :price, :stop_price, :submitted_at,
                        CAST(:source_metadata AS jsonb), NOW()
                    )
                    ON CONFLICT (trading_account_id, platform_order_ref)
                    DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        side = EXCLUDED.side,
                        order_type = EXCLUDED.order_type,
                        status = EXCLUDED.status,
                        quantity = EXCLUDED.quantity,
                        filled_quantity = EXCLUDED.filled_quantity,
                        price = EXCLUDED.price,
                        stop_price = EXCLUDED.stop_price,
                        submitted_at = COALESCE(EXCLUDED.submitted_at, canonical_orders.submitted_at),
                        source_metadata = canonical_orders.source_metadata || EXCLUDED.source_metadata,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    """
                    ),
                    {
                        "trading_account_id": trading_account["id"],
                        "platform_order_ref": order.get("platform_order_ref") or order.get("id"),
                        "symbol": order.get("symbol"),
                        "side": order.get("side"),
                        "order_type": order.get("order_type"),
                        "status": order.get("status"),
                        "quantity": order.get("quantity"),
                        "filled_quantity": order.get("filled_quantity"),
                        "price": order.get("price"),
                        "stop_price": order.get("stop_price"),
                        "submitted_at": order.get("submitted_at"),
                        "source_metadata": _json(order.get("source_metadata") or {}),
                    },
                )

        synced_accounts.append(
            {
                "trading_account_id": trading_account["id"],
                "external_account_id": trading_account["external_account_id"],
                "extension_device_id": trading_account.get("extension_device_id"),
                "platform_session_id": trading_account.get("platform_session_id"),
                "execution_enabled": trading_account.get("execution_enabled"),
            }
        )

    return {"ok": True, "synced_accounts": synced_accounts}


async def create_execution_batch(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with engine.begin() as conn:
        batch = (
            await conn.execute(
                text(
                    """
            INSERT INTO execution_batches (
                user_id, request_id, status, requested_by, metadata
            ) VALUES (
                :user_id, :request_id, 'queued', :requested_by, CAST(:metadata AS jsonb)
            ) RETURNING *
            """
                ),
                {
                    "user_id": user_id,
                    "request_id": payload.get("request_id") or secrets.token_hex(8),
                    "requested_by": payload.get("requested_by") or "web_app",
                    "metadata": _json(payload.get("metadata") or {}),
                },
            )
        ).mappings().first()

        commands: list[dict[str, Any]] = []
        for command in payload.get("commands") or []:
            row = (
                await conn.execute(
                    text(
                        """
                INSERT INTO execution_commands (
                    execution_batch_id, user_id, trading_account_id, extension_device_id, platform_session_id,
                    adapter_key, command_type, status, payload, expires_at
                ) VALUES (
                    :execution_batch_id, :user_id, :trading_account_id, :extension_device_id, :platform_session_id,
                    :adapter_key, :command_type, 'queued', CAST(:payload AS jsonb), :expires_at
                )
                RETURNING *
                """
                    ),
                    {
                        "execution_batch_id": batch["id"],
                        "user_id": user_id,
                        "trading_account_id": command.get("trading_account_id"),
                        "extension_device_id": command.get("extension_device_id"),
                        "platform_session_id": command.get("platform_session_id"),
                        "adapter_key": command.get("adapter_key"),
                        "command_type": command.get("command_type"),
                        "payload": _json(command.get("payload") or {}),
                        "expires_at": command.get("expires_at"),
                    },
                )
            ).mappings().first()
            commands.append(dict(row))

    return {"batch_id": batch["id"], "commands": commands}


async def poll_execution_commands(extension_ctx: dict[str, Any], adapter_keys: list[str] | None = None) -> list[dict[str, Any]]:
    lease_owner = f"session:{extension_ctx['extension_session_id']}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
            UPDATE execution_commands
            SET status = 'queued',
                dispatch_lease_owner = NULL,
                dispatch_lease_expires_at = NULL,
                updated_at = NOW()
            WHERE user_id = :user_id
              AND extension_device_id = :extension_device_id
              AND status = 'dispatched'
              AND dispatch_lease_expires_at IS NOT NULL
              AND dispatch_lease_expires_at < NOW()
              AND (expires_at IS NULL OR expires_at > NOW())
            """
            ),
            {
                "user_id": extension_ctx["user_id"],
                "extension_device_id": extension_ctx["extension_device_id"],
            },
        )

        rows = (
            await conn.execute(
                text(
                    """
            UPDATE execution_commands
            SET status = 'dispatched',
                dispatched_at = NOW(),
                dispatch_lease_owner = :lease_owner,
                dispatch_lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                updated_at = NOW()
            WHERE id IN (
                SELECT id FROM execution_commands
                WHERE user_id = :user_id
                  AND extension_device_id = :extension_device_id
                  AND status = 'queued'
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND (:adapter_keys_is_null OR adapter_key = ANY(:adapter_keys))
                ORDER BY created_at ASC
                LIMIT :limit
            )
            RETURNING *
            """
                ),
                {
                    "lease_owner": lease_owner,
                    "lease_seconds": DISPATCH_LEASE_SECONDS,
                    "user_id": extension_ctx["user_id"],
                    "extension_device_id": extension_ctx["extension_device_id"],
                    "adapter_keys": adapter_keys or [],
                    "adapter_keys_is_null": adapter_keys is None,
                    "limit": COMMAND_POLL_LIMIT,
                },
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def ack_execution_command(extension_ctx: dict[str, Any], command_id: int, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if status not in {"acked", "running"}:
        raise HTTPException(status_code=422, detail="Invalid ack status")
    async with engine.begin() as conn:
        current = (
            await conn.execute(
                text(
                    """
                SELECT status FROM execution_commands
                WHERE id = :id AND user_id = :user_id AND extension_device_id = :extension_device_id
                """
                ),
                {
                    "id": command_id,
                    "user_id": extension_ctx["user_id"],
                    "extension_device_id": extension_ctx["extension_device_id"],
                },
            )
        ).mappings().first()
        if not current:
            raise HTTPException(status_code=404, detail="Command not found")
        if not validate_command_transition(current["status"], status):
            raise HTTPException(status_code=409, detail=f"Cannot transition {current['status']} -> {status}")

        row = (
            await conn.execute(
                text(
                    """
                UPDATE execution_commands
                SET status = :status,
                    acked_at = CASE WHEN :status = 'acked' THEN NOW() ELSE acked_at END,
                    started_at = CASE WHEN :status = 'running' THEN NOW() ELSE started_at END,
                    dispatch_lease_owner = NULL,
                    dispatch_lease_expires_at = NULL,
                    metadata = metadata || :metadata::jsonb,
                    updated_at = NOW()
                WHERE id = :id
                RETURNING *
                """
                ),
                {"id": command_id, "status": status, "metadata": _json(metadata)},
            )
        ).mappings().first()
    return dict(row)


async def ingest_execution_result(extension_ctx: dict[str, Any], command_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    final_status = payload.get("status")
    if final_status not in COMMAND_TERMINAL_STATUSES:
        raise HTTPException(status_code=422, detail="Result status must be terminal")

    async with engine.begin() as conn:
        command = (
            await conn.execute(
                text(
                    """
                SELECT * FROM execution_commands
                WHERE id = :id AND user_id = :user_id AND extension_device_id = :extension_device_id
                """
                ),
                {
                    "id": command_id,
                    "user_id": extension_ctx["user_id"],
                    "extension_device_id": extension_ctx["extension_device_id"],
                },
            )
        ).mappings().first()
        if not command:
            raise HTTPException(status_code=404, detail="Command not found")
        if not validate_command_transition(command["status"], final_status):
            raise HTTPException(status_code=409, detail=f"Cannot transition {command['status']} -> {final_status}")

        result = (
            await conn.execute(
                text(
                    """
                INSERT INTO execution_results (
                    execution_command_id, user_id, status, result_payload, adapter_error_code,
                    adapter_error_message, received_at
                ) VALUES (
                    :execution_command_id, :user_id, :status, CAST(:result_payload AS jsonb), :adapter_error_code,
                    :adapter_error_message, NOW()
                ) RETURNING *
                """
                ),
                {
                    "execution_command_id": command_id,
                    "user_id": extension_ctx["user_id"],
                    "status": final_status,
                    "result_payload": _json(payload.get("result_payload") or {}),
                    "adapter_error_code": payload.get("adapter_error_code"),
                    "adapter_error_message": payload.get("adapter_error_message"),
                },
            )
        ).mappings().first()

        await conn.execute(
            text(
                """
            UPDATE execution_commands
            SET status = :status,
                completed_at = NOW(),
                dispatch_lease_owner = NULL,
                dispatch_lease_expires_at = NULL,
                updated_at = NOW()
            WHERE id = :id
            """
            ),
            {"status": final_status, "id": command_id},
        )
    return {"command_id": command_id, "status": final_status, "result_id": result["id"]}
