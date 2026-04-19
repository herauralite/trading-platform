import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any
import hashlib
import httpx
import json
import os
import socket
import uuid

from sqlalchemy import text

from app.core.database import engine

SNAPSHOT_DEDUPE_WINDOW_SECONDS = 30
USER_SCOPED_CONNECTORS = {"manual", "csv_import"}
DEFAULT_CONNECTOR_STATUS = "connected"
ALLOWED_CONNECTOR_STATUSES = {"connected", "degraded", "disconnected", "sync_error"}
ALLOWED_CONNECTOR_STATUSES.update({"sync_queued", "sync_running", "sync_retrying", "awaiting_alerts", "active", "bridge_required", "waiting_for_registration", "ready_for_account_attach", "beta_pending", "metadata_saved", "awaiting_secure_auth", "waiting_for_secure_auth_support", "account_verified", "paper_connected", "live_connected", "validation_failed"})
SYNC_RUN_FINAL_STATUSES = {"succeeded", "failed"}
SYNC_RUN_RETRY_DELAYS_SECONDS = [2, 5]
SYNC_RUN_LEASE_SECONDS = 300
SYNC_WORKER_IDLE_POLL_SECONDS = 1.0
SYNC_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
FUNDINGPIPS_SYNC_FRESHNESS_SLA_MINUTES = 15
DEFAULT_EXTERNAL_HEALTHCHECK_TIMEOUT_SECONDS = 8.0


class ConnectorSyncError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        category: str,
        transient: bool,
        status_detail: str | None = None,
        source_summary: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.transient = transient
        self.status_detail = status_detail or message
        self.source_summary = source_summary or {}
        self.diagnostics = diagnostics or {}

    def to_result_detail(self) -> dict[str, Any]:
        return {
            "result_category": "error",
            "error_code": self.code,
            "error_category": self.category,
            "is_transient": self.transient,
            "status_detail": self.status_detail,
            "source_summary": self.source_summary,
            "diagnostics": self.diagnostics,
        }


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
            CREATE TABLE IF NOT EXISTS connector_lifecycle (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'connected',
                is_connected BOOLEAN NOT NULL DEFAULT TRUE,
                last_connected_at TIMESTAMPTZ,
                last_disconnected_at TIMESTAMPTZ,
                last_sync_at TIMESTAMPTZ,
                last_activity_at TIMESTAMPTZ,
                last_error TEXT,
                last_error_at TIMESTAMPTZ,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, connector_type)
            )
        """))
        await conn.execute(text("ALTER TABLE connector_lifecycle ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE connector_lifecycle ADD COLUMN IF NOT EXISTS last_error TEXT"))
        await conn.execute(text("ALTER TABLE connector_lifecycle ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS connector_lifecycle_user_idx ON connector_lifecycle(user_id)"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS connector_sync_runs (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                trigger TEXT NOT NULL DEFAULT 'manual',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2,
                next_retry_at TIMESTAMPTZ,
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                error_detail TEXT,
                result_detail JSONB DEFAULT '{}'::jsonb,
                metadata JSONB DEFAULT '{}'::jsonb
            )
        """))
        await conn.execute(text("ALTER TABLE connector_sync_runs ADD COLUMN IF NOT EXISTS lease_owner TEXT"))
        await conn.execute(text("ALTER TABLE connector_sync_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS connector_sync_runs_user_connector_idx ON connector_sync_runs(user_id, connector_type, created_at DESC)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS connector_sync_runs_pending_idx ON connector_sync_runs(status, next_retry_at, created_at)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS connector_sync_runs_lease_idx ON connector_sync_runs(status, lease_expires_at)"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS connector_configs (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'incomplete',
                non_secret_config JSONB DEFAULT '{}'::jsonb,
                secret_config JSONB DEFAULT '{}'::jsonb,
                validation_error TEXT,
                configured_at TIMESTAMPTZ,
                rotated_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, connector_type)
            )
        """))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'incomplete'"))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS non_secret_config JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS secret_config JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS validation_error TEXT"))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS configured_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE connector_configs ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMPTZ"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS connector_configs_user_idx ON connector_configs(user_id)"))

        # 1) Create core tables first (fresh DB safe).
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
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mt5_bridge_accounts (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                trading_account_id INTEGER NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
                external_account_id TEXT NOT NULL,
                bridge_status TEXT NOT NULL DEFAULT 'bridge_required',
                bridge_url TEXT,
                mt5_server TEXT,
                last_bridge_sync_at TIMESTAMPTZ,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, trading_account_id)
            )
        """))
        await conn.execute(text("ALTER TABLE mt5_bridge_accounts ADD COLUMN IF NOT EXISTS bridge_status TEXT NOT NULL DEFAULT 'bridge_required'"))
        await conn.execute(text("ALTER TABLE mt5_bridge_accounts ADD COLUMN IF NOT EXISTS bridge_url TEXT"))
        await conn.execute(text("ALTER TABLE mt5_bridge_accounts ADD COLUMN IF NOT EXISTS mt5_server TEXT"))
        await conn.execute(text("ALTER TABLE mt5_bridge_accounts ADD COLUMN IF NOT EXISTS last_bridge_sync_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE mt5_bridge_accounts ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS mt5_bridge_accounts_user_idx ON mt5_bridge_accounts(user_id)"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mt5_pairing_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_hint TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_external_account_id TEXT,
                requested_mt5_server TEXT,
                requested_bridge_url TEXT,
                requested_display_name TEXT,
                expires_at TIMESTAMPTZ NOT NULL,
                consumed_at TIMESTAMPTZ,
                trusted_bridge_id TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS token_hint TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS requested_external_account_id TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS requested_mt5_server TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS requested_bridge_url TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS requested_display_name TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS trusted_bridge_id TEXT"))
        await conn.execute(text("ALTER TABLE mt5_pairing_tokens ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS mt5_pairing_tokens_user_idx ON mt5_pairing_tokens(user_id, created_at DESC)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS mt5_pairing_tokens_status_idx ON mt5_pairing_tokens(status, expires_at)"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mt5_trusted_bridges (
                id BIGSERIAL PRIMARY KEY,
                bridge_id TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                pairing_token_id BIGINT REFERENCES mt5_pairing_tokens(id) ON DELETE SET NULL,
                display_name TEXT,
                machine_label TEXT,
                bridge_secret_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'registered',
                last_heartbeat_at TIMESTAMPTZ,
                last_seen_ip TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS display_name TEXT"))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS machine_label TEXT"))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'registered'"))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS last_seen_ip TEXT"))
        await conn.execute(text("ALTER TABLE mt5_trusted_bridges ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS mt5_trusted_bridges_user_idx ON mt5_trusted_bridges(user_id, updated_at DESC)"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tradingview_webhook_connections (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                trading_account_id INTEGER NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
                display_label TEXT,
                account_alias TEXT,
                webhook_token_hash TEXT NOT NULL UNIQUE,
                webhook_token_hint TEXT NOT NULL,
                activation_state TEXT NOT NULL DEFAULT 'awaiting_alerts',
                last_event_at TIMESTAMPTZ,
                last_event_payload JSONB DEFAULT '{}'::jsonb,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("ALTER TABLE tradingview_webhook_connections ADD COLUMN IF NOT EXISTS account_alias TEXT"))
        await conn.execute(text("ALTER TABLE tradingview_webhook_connections ADD COLUMN IF NOT EXISTS activation_state TEXT NOT NULL DEFAULT 'awaiting_alerts'"))
        await conn.execute(text("ALTER TABLE tradingview_webhook_connections ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE tradingview_webhook_connections ADD COLUMN IF NOT EXISTS last_event_payload JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE tradingview_webhook_connections ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS tv_webhook_user_idx ON tradingview_webhook_connections(user_id, created_at DESC)"))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS public_api_beta_connections (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                trading_account_id INTEGER NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
                display_label TEXT,
                environment TEXT,
                account_alias TEXT,
                beta_state TEXT NOT NULL DEFAULT 'beta_pending',
                encrypted_api_key TEXT,
                encrypted_api_secret TEXT,
                account_summary JSONB DEFAULT '{}'::jsonb,
                last_validation_error TEXT,
                last_validated_at TIMESTAMPTZ,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS environment TEXT"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS account_alias TEXT"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS beta_state TEXT NOT NULL DEFAULT 'beta_pending'"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS encrypted_api_key TEXT"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS encrypted_api_secret TEXT"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS account_summary JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS last_validation_error TEXT"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE public_api_beta_connections ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS public_api_beta_user_idx ON public_api_beta_connections(user_id, connector_type, created_at DESC)"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS public_api_beta_trading_account_uq ON public_api_beta_connections(trading_account_id)"))

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
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS position_key TEXT"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT NOW()"))
        await conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ"))
        # Ensure position_key is always present so ON CONFLICT target is deterministic.
        await conn.execute(text("""
            UPDATE positions
            SET position_key = CONCAT(
                COALESCE(UPPER(symbol), 'UNKNOWN'),
                '|',
                COALESCE(LOWER(side), 'unknown'),
                '|',
                COALESCE(to_char(opened_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF'), CONCAT('legacy-', id::text))
            )
            WHERE position_key IS NULL
        """))
        await conn.execute(text("ALTER TABLE positions ALTER COLUMN position_key SET NOT NULL"))
        # Neutralize legacy uniqueness that conflicts with position_key identity.
        await conn.execute(text("ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_trading_account_id_symbol_side_key"))
        await conn.execute(text("DROP INDEX IF EXISTS positions_trading_account_id_symbol_side_key"))

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

        # 2) Create/repair indexes & constraints needed by new logic.
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS trading_accounts_account_key_uq ON trading_accounts(account_key)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS account_snapshots_account_time_idx ON account_snapshots(trading_account_id, snapshot_time DESC)"))
        await conn.execute(text("DROP INDEX IF EXISTS positions_account_position_key_uq"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS positions_account_position_key_uq ON positions(trading_account_id, position_key)"))

        # 3) Backfill + dedupe/rewire logic once all referenced tables exist.
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
    if user_id:
        await upsert_connector_lifecycle(
            user_id=user_id,
            connector_type=connector_type,
            status="connected",
            is_connected=True,
            last_activity_at=datetime.now(timezone.utc),
            metadata={"source": "upsert_trading_account"},
        )
    return dict(row)


async def _set_sync_run_status(
    run_id: int,
    *,
    status: str,
    expected_status: str | None = None,
    lease_owner: str | None = None,
    clear_lease: bool = False,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    retry_count: int | None = None,
    next_retry_at: datetime | None = None,
    error_detail: str | None = None,
    result_detail: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = {
        "id": run_id,
        "status": status,
        "expected_status": expected_status,
        "lease_owner": lease_owner,
        "clear_lease": clear_lease,
        "started_at": started_at,
        "finished_at": finished_at,
        "retry_count": retry_count,
        "next_retry_at": next_retry_at,
        "error_detail": error_detail,
        "result_detail": json.dumps(result_detail or {}),
        "metadata": json.dumps(metadata or {}),
    }
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            UPDATE connector_sync_runs
            SET
                status = :status,
                started_at = COALESCE(:started_at, started_at),
                finished_at = COALESCE(:finished_at, finished_at),
                retry_count = COALESCE(:retry_count, retry_count),
                next_retry_at = :next_retry_at,
                lease_owner = CASE
                    WHEN :clear_lease THEN NULL
                    WHEN :lease_owner IS NULL THEN lease_owner
                    ELSE :lease_owner
                END,
                lease_expires_at = CASE
                    WHEN :clear_lease THEN NULL
                    ELSE lease_expires_at
                END,
                error_detail = :error_detail,
                result_detail = CASE
                    WHEN :result_detail::jsonb = '{}'::jsonb THEN result_detail
                    ELSE result_detail || :result_detail::jsonb
                END,
                metadata = metadata || :metadata::jsonb
            WHERE id = :id
              AND (:expected_status IS NULL OR status = :expected_status)
              AND (:lease_owner IS NULL OR lease_owner = :lease_owner)
            RETURNING *
        """), params)).mappings().first()
    if not row:
        raise ValueError(f"sync run not found: {run_id}")
    return dict(row)


async def create_connector_sync_run(
    user_id: str,
    connector_type: str,
    *,
    trigger: str = "manual",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_user = _normalize_user_id(user_id)
    if not normalized_user:
        raise ValueError("user_id is required")
    normalized_connector = _normalize_connector(connector_type)
    async with engine.begin() as conn:
        run = (await conn.execute(text("""
            INSERT INTO connector_sync_runs (
                user_id, connector_type, status, trigger, metadata
            ) VALUES (
                :user_id, :connector_type, 'queued', :trigger, CAST(:metadata AS jsonb)
            )
            RETURNING *
        """), {
            "user_id": normalized_user,
            "connector_type": normalized_connector,
            "trigger": trigger,
            "metadata": json.dumps(metadata or {}),
        })).mappings().first()
    await upsert_connector_lifecycle(
        user_id=normalized_user,
        connector_type=normalized_connector,
        status="sync_queued",
        is_connected=True,
        last_activity_at=datetime.now(timezone.utc),
        metadata={"sync_run_id": run["id"], "sync_state": "queued"},
    )
    return dict(run)


async def get_connector_sync_runs(
    user_id: str,
    connector_type: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT *
            FROM connector_sync_runs
            WHERE user_id = :user_id AND connector_type = :connector_type
            ORDER BY created_at DESC
            LIMIT :limit
        """), {
            "user_id": _normalize_user_id(user_id),
            "connector_type": _normalize_connector(connector_type),
            "limit": max(1, min(limit, 50)),
        })).mappings().all()
    return [dict(row) for row in rows]


async def get_latest_connector_sync_run(user_id: str, connector_type: str) -> dict[str, Any] | None:
    rows = await get_connector_sync_runs(user_id, connector_type, limit=1)
    return rows[0] if rows else None


async def _perform_fundingpips_sync(run: dict[str, Any]) -> dict[str, Any]:
    config_row = await get_connector_config(run["user_id"], run["connector_type"], include_secret=True)
    if config_row and (config_row.get("status") == "configured"):
        return await _perform_fundingpips_external_probe(run, config_row)
    if config_row and (config_row.get("status") in {"invalid", "incomplete"}):
        raise ConnectorSyncError(
            "FundingPips connector configuration is incomplete",
            code="connector_config_incomplete",
            category="configuration",
            transient=False,
            status_detail=config_row.get("validation_error") or "Connector configuration is incomplete.",
            source_summary={"connector_mode": "external_probe"},
        )

    async with engine.connect() as conn:
        account_rows = (await conn.execute(text("""
            SELECT
                ta.id,
                ta.external_account_id,
                ta.display_label,
                MAX(s.snapshot_time) AS last_snapshot_at,
                COUNT(*) FILTER (WHERE p.is_active = TRUE) AS open_positions
            FROM trading_accounts ta
            LEFT JOIN account_snapshots s ON s.trading_account_id = ta.id
            LEFT JOIN positions p ON p.trading_account_id = ta.id
            WHERE ta.user_id = :uid
              AND ta.connector_type = :connector
              AND ta.is_active = TRUE
            GROUP BY ta.id, ta.external_account_id, ta.display_label
            ORDER BY ta.id
        """), {"uid": run["user_id"], "connector": run["connector_type"]})).mappings().all()
        trades_row = (await conn.execute(text("""
            SELECT COUNT(*) AS total
            FROM trades
            WHERE user_id = :uid
              AND connector_type = :connector
              AND close_time >= NOW() - INTERVAL '24 hours'
        """), {"uid": run["user_id"], "connector": run["connector_type"]})).mappings().first()
        event_row = (await conn.execute(text("""
            SELECT COUNT(*) AS total
            FROM connector_events
            WHERE user_id = :uid
              AND connector_type = :connector
              AND event_time >= NOW() - INTERVAL '24 hours'
        """), {"uid": run["user_id"], "connector": run["connector_type"]})).mappings().first()

    accounts = [dict(row) for row in account_rows]
    if not accounts:
        raise ConnectorSyncError(
            "No active FundingPips accounts available for sync",
            code="no_active_accounts",
            category="configuration",
            transient=False,
            status_detail="No active FundingPips accounts are linked for this user.",
            source_summary={"connector_mode": "extension_push"},
        )

    now = datetime.now(timezone.utc)
    freshness_sla = timedelta(minutes=FUNDINGPIPS_SYNC_FRESHNESS_SLA_MINUTES)
    fresh_accounts = 0
    stale_accounts: list[dict[str, Any]] = []
    total_open_positions = 0
    freshest_snapshot_at = None

    for account in accounts:
        snapshot_at = _parse_dt(account.get("last_snapshot_at"))
        open_positions = int(account.get("open_positions") or 0)
        total_open_positions += open_positions
        if snapshot_at and (freshest_snapshot_at is None or snapshot_at > freshest_snapshot_at):
            freshest_snapshot_at = snapshot_at
        is_fresh = bool(snapshot_at and (now - snapshot_at) <= freshness_sla)
        if is_fresh:
            fresh_accounts += 1
            continue
        stale_for_minutes = int((now - snapshot_at).total_seconds() // 60) if snapshot_at else None
        stale_accounts.append({
            "account_id": account["external_account_id"],
            "display_label": account.get("display_label"),
            "last_snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
            "stale_for_minutes": stale_for_minutes,
            "open_positions": open_positions,
        })

    counts = {
        "accounts_total": len(accounts),
        "accounts_fresh": fresh_accounts,
        "accounts_stale": len(stale_accounts),
        "open_positions": total_open_positions,
        "trades_24h": int((trades_row or {}).get("total") or 0),
        "events_24h": int((event_row or {}).get("total") or 0),
    }
    source_summary = {
        "connector_mode": "extension_push",
        "freshness_sla_minutes": FUNDINGPIPS_SYNC_FRESHNESS_SLA_MINUTES,
        "freshest_snapshot_at": freshest_snapshot_at.isoformat() if freshest_snapshot_at else None,
        "stale_account_ids": [a["account_id"] for a in stale_accounts],
    }
    if fresh_accounts == 0:
        raise ConnectorSyncError(
            "FundingPips account data is stale; no recent snapshots were observed",
            code="stale_source_data",
            category="source_staleness",
            transient=True,
            status_detail="No FundingPips accounts have recent snapshots. Ensure the extension is online and connected.",
            source_summary=source_summary,
            diagnostics={"counts": counts, "stale_accounts": stale_accounts},
        )

    return {
        "result_category": "connector_sync_summary",
        "connector_type": run["connector_type"],
        "status_detail": f"FundingPips sync checked {counts['accounts_total']} accounts; {counts['accounts_fresh']} account(s) are fresh.",
        "counts": counts,
        "source_summary": source_summary,
        "warnings": [{
            "code": "stale_accounts_detected",
            "status_detail": f"{len(stale_accounts)} account(s) have stale snapshots.",
            "account_ids": source_summary["stale_account_ids"],
        }] if stale_accounts else [],
    }


async def _perform_connector_sync(run: dict[str, Any]) -> dict[str, Any]:
    connector = run["connector_type"]
    if connector == "manual":
        raise ConnectorSyncError(
            "Manual connector cannot execute remote sync",
            code="unsupported_live_sync_connector",
            category="not_supported",
            transient=False,
            status_detail="Manual connector does not support remote sync execution.",
        )
    if connector == "fundingpips_extension":
        return await _perform_fundingpips_sync(run)
    raise ConnectorSyncError(
        f"Unsupported live sync connector: {connector}",
        code="unsupported_live_sync_connector",
        category="not_supported",
        transient=False,
        status_detail=f"No connector-specific sync executor is registered for '{connector}'.",
    )


async def claim_next_connector_sync_run(
    *,
    worker_id: str = SYNC_WORKER_ID,
    lease_seconds: int = SYNC_RUN_LEASE_SECONDS,
) -> dict[str, Any] | None:
    async with engine.begin() as conn:
        claimed = (await conn.execute(text("""
            WITH candidate AS (
                SELECT id, status
                FROM connector_sync_runs
                WHERE
                    status = 'queued'
                    OR (status = 'retrying' AND (next_retry_at IS NULL OR next_retry_at <= NOW()))
                    OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= NOW())
                ORDER BY
                    CASE WHEN status = 'running' THEN 0 WHEN status = 'retrying' THEN 1 ELSE 2 END,
                    COALESCE(next_retry_at, created_at),
                    created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE connector_sync_runs r
            SET
                status = 'running',
                started_at = COALESCE(r.started_at, NOW()),
                next_retry_at = NULL,
                lease_owner = :worker_id,
                lease_expires_at = NOW() + (:lease_seconds || ' seconds')::interval,
                metadata = r.metadata || jsonb_build_object(
                    'claimed_by', :worker_id,
                    'claimed_at', NOW(),
                    'claim_from_status', candidate.status
                )
            FROM candidate
            WHERE r.id = candidate.id
            RETURNING r.*
        """), {"worker_id": worker_id, "lease_seconds": max(30, lease_seconds)})).mappings().first()
    return dict(claimed) if claimed else None


async def execute_connector_sync_run(run_id: int, *, worker_id: str = SYNC_WORKER_ID) -> dict[str, Any]:
    async with engine.connect() as conn:
        run_row = (await conn.execute(text("""
            SELECT *
            FROM connector_sync_runs
            WHERE id = :id
            LIMIT 1
        """), {"id": run_id})).mappings().first()
    if not run_row:
        raise ValueError(f"sync run not found: {run_id}")

    run = dict(run_row)
    if run.get("status") != "running":
        raise RuntimeError(f"sync run {run_id} must be claimed before execution")

    attempt = int(run.get("retry_count") or 0)
    max_retries = int(run.get("max_retries") or 0)
    now = datetime.now(timezone.utc)
    await _set_sync_run_status(
        run_id,
        status="running",
        expected_status="running",
        lease_owner=worker_id,
        started_at=now,
        retry_count=attempt,
        next_retry_at=None,
        error_detail=None,
    )
    await upsert_connector_lifecycle(
        user_id=run["user_id"],
        connector_type=run["connector_type"],
        status="sync_running",
        is_connected=True,
        last_activity_at=now,
        metadata={"sync_run_id": run_id, "sync_state": "running", "attempt": attempt + 1},
    )

    try:
        result_detail = await _perform_connector_sync(run)
        finished = datetime.now(timezone.utc)
        row = await _set_sync_run_status(
            run_id,
            status="succeeded",
            expected_status="running",
            lease_owner=worker_id,
            clear_lease=True,
            finished_at=finished,
            retry_count=attempt,
            result_detail=result_detail,
        )
        await upsert_connector_lifecycle(
            user_id=run["user_id"],
            connector_type=run["connector_type"],
            status="connected",
            is_connected=True,
            last_sync_at=finished,
            last_activity_at=finished,
            error=None,
            metadata={"sync_run_id": run_id, "sync_state": "succeeded"},
        )
        return row
    except Exception as exc:
        sync_error = exc if isinstance(exc, ConnectorSyncError) else ConnectorSyncError(
            f"Unexpected sync execution failure: {exc}",
            code="unexpected_exception",
            category="internal",
            transient=True,
            status_detail="Unexpected connector sync failure during execution.",
            diagnostics={"exception_type": exc.__class__.__name__},
        )
        err = str(sync_error)
        result_detail = sync_error.to_result_detail()
        should_retry = sync_error.transient and attempt < max_retries
        if should_retry:
            delay = SYNC_RUN_RETRY_DELAYS_SECONDS[min(attempt, len(SYNC_RUN_RETRY_DELAYS_SECONDS) - 1)]
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            row = await _set_sync_run_status(
                run_id,
                status="retrying",
                expected_status="running",
                lease_owner=worker_id,
                clear_lease=True,
                retry_count=attempt + 1,
                next_retry_at=retry_at,
                error_detail=err,
                result_detail=result_detail,
                metadata={"last_retry_delay_seconds": delay},
            )
            await upsert_connector_lifecycle(
                user_id=run["user_id"],
                connector_type=run["connector_type"],
                status="sync_retrying",
                is_connected=True,
                last_activity_at=datetime.now(timezone.utc),
                error=err,
                metadata={"sync_run_id": run_id, "sync_state": "retrying", "next_retry_at": retry_at.isoformat()},
            )
            return row
        finished = datetime.now(timezone.utc)
        row = await _set_sync_run_status(
            run_id,
            status="failed",
            expected_status="running",
            lease_owner=worker_id,
            clear_lease=True,
            finished_at=finished,
            retry_count=attempt,
            next_retry_at=None,
            error_detail=err,
            result_detail=result_detail,
        )
        await upsert_connector_lifecycle(
            user_id=run["user_id"],
            connector_type=run["connector_type"],
            status="sync_error",
            is_connected=True,
            last_activity_at=finished,
            error=err,
            metadata={"sync_run_id": run_id, "sync_state": "failed"},
        )
        return row


async def run_connector_sync_once(*, worker_id: str = SYNC_WORKER_ID) -> dict[str, Any] | None:
    claimed = await claim_next_connector_sync_run(worker_id=worker_id)
    if not claimed:
        return None
    return await execute_connector_sync_run(int(claimed["id"]), worker_id=worker_id)


async def connector_sync_worker_loop(
    stop_event: asyncio.Event,
    *,
    worker_id: str = SYNC_WORKER_ID,
    idle_poll_seconds: float = SYNC_WORKER_IDLE_POLL_SECONDS,
) -> None:
    while not stop_event.is_set():
        handled = await run_connector_sync_once(worker_id=worker_id)
        if handled:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.1, idle_poll_seconds))
        except asyncio.TimeoutError:
            continue


async def enqueue_connector_sync_run(
    user_id: str,
    connector_type: str,
    *,
    trigger: str = "manual",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await create_connector_sync_run(user_id, connector_type, trigger=trigger, metadata=metadata)


async def upsert_connector_lifecycle(
    user_id: str,
    connector_type: str,
    *,
    status: str | None = None,
    is_connected: bool | None = None,
    last_sync_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_connector = _normalize_connector(connector_type)
    normalized_user = _normalize_user_id(user_id)
    if not normalized_user:
        raise ValueError("user_id is required for connector lifecycle")

    desired_status = (status or DEFAULT_CONNECTOR_STATUS).strip().lower()
    if desired_status not in ALLOWED_CONNECTOR_STATUSES:
        desired_status = DEFAULT_CONNECTOR_STATUS

    now = datetime.now(timezone.utc)
    params = {
        "user_id": normalized_user,
        "connector_type": normalized_connector,
        "status": desired_status,
        "is_connected": bool(is_connected) if is_connected is not None else desired_status != "disconnected",
        "last_connected_at": now if (is_connected is True or desired_status in {"connected", "degraded", "sync_error"}) else None,
        "last_disconnected_at": now if (is_connected is False or desired_status == "disconnected") else None,
        "last_sync_at": last_sync_at,
        "last_activity_at": last_activity_at,
        "last_error": error,
        "last_error_at": now if error else None,
        "metadata": json.dumps(metadata or {}),
    }
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO connector_lifecycle (
                user_id, connector_type, status, is_connected, last_connected_at,
                last_disconnected_at, last_sync_at, last_activity_at, last_error, last_error_at, metadata
            ) VALUES (
                :user_id, :connector_type, :status, :is_connected, :last_connected_at,
                :last_disconnected_at, :last_sync_at, :last_activity_at, :last_error, :last_error_at, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (user_id, connector_type)
            DO UPDATE SET
                status = EXCLUDED.status,
                is_connected = EXCLUDED.is_connected,
                last_connected_at = COALESCE(EXCLUDED.last_connected_at, connector_lifecycle.last_connected_at),
                last_disconnected_at = COALESCE(EXCLUDED.last_disconnected_at, connector_lifecycle.last_disconnected_at),
                last_sync_at = COALESCE(EXCLUDED.last_sync_at, connector_lifecycle.last_sync_at),
                last_activity_at = COALESCE(EXCLUDED.last_activity_at, connector_lifecycle.last_activity_at),
                last_error = EXCLUDED.last_error,
                last_error_at = COALESCE(EXCLUDED.last_error_at, connector_lifecycle.last_error_at),
                metadata = connector_lifecycle.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING *
        """), params)).mappings().first()
    return dict(row)


async def get_connector_lifecycle(user_id: str, connector_type: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT *
            FROM connector_lifecycle
            WHERE user_id = :user_id AND connector_type = :connector_type
            LIMIT 1
        """), {
            "user_id": _normalize_user_id(user_id),
            "connector_type": _normalize_connector(connector_type),
        })).mappings().first()
    return dict(row) if row else None


def _sanitize_connector_config(
    row: dict[str, Any] | None,
    *,
    non_secret_fields: list[str] | None = None,
    secret_fields: list[str] | None = None,
) -> dict[str, Any] | None:
    if not row:
        return None
    non_secret = dict(row.get("non_secret_config") or {})
    if non_secret_fields:
        non_secret = {k: non_secret.get(k) for k in non_secret_fields if k in non_secret}
    stored_secret = dict(row.get("secret_config") or {})
    allowed_secret_fields = secret_fields or list(stored_secret.keys())
    stored_secret_keys = [field for field in allowed_secret_fields if stored_secret.get(field)]
    return {
        "user_id": row["user_id"],
        "connector_type": row["connector_type"],
        "status": row.get("status") or "incomplete",
        "non_secret_config": non_secret,
        "has_secret_config": bool(stored_secret_keys),
        "configured_secret_fields": stored_secret_keys,
        "validation_error": row.get("validation_error"),
        "configured_at": row.get("configured_at"),
        "rotated_at": row.get("rotated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def get_connector_config(
    user_id: str,
    connector_type: str,
    *,
    include_secret: bool = False,
) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT *
            FROM connector_configs
            WHERE user_id = :user_id
              AND connector_type = :connector_type
            LIMIT 1
        """), {
            "user_id": _normalize_user_id(user_id),
            "connector_type": _normalize_connector(connector_type),
        })).mappings().first()
    if not row:
        return None
    data = dict(row)
    if include_secret:
        return data
    return _sanitize_connector_config(data)


async def upsert_connector_config(
    user_id: str,
    connector_type: str,
    *,
    non_secret_config: dict[str, Any] | None = None,
    secret_config: dict[str, Any] | None = None,
    status: str | None = None,
    validation_error: str | None = None,
) -> dict[str, Any]:
    normalized_user = _normalize_user_id(user_id)
    if not normalized_user:
        raise ValueError("user_id is required for connector config")
    normalized_connector = _normalize_connector(connector_type)
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO connector_configs (
                user_id,
                connector_type,
                status,
                non_secret_config,
                secret_config,
                validation_error,
                configured_at,
                rotated_at,
                created_at,
                updated_at
            ) VALUES (
                :user_id,
                :connector_type,
                :status,
                CAST(:non_secret_config AS jsonb),
                CAST(:secret_config AS jsonb),
                :validation_error,
                :configured_at,
                :rotated_at,
                NOW(),
                NOW()
            )
            ON CONFLICT (user_id, connector_type)
            DO UPDATE SET
                status = COALESCE(EXCLUDED.status, connector_configs.status),
                non_secret_config = connector_configs.non_secret_config || EXCLUDED.non_secret_config,
                secret_config = connector_configs.secret_config || EXCLUDED.secret_config,
                validation_error = EXCLUDED.validation_error,
                configured_at = COALESCE(EXCLUDED.configured_at, connector_configs.configured_at),
                rotated_at = COALESCE(EXCLUDED.rotated_at, connector_configs.rotated_at),
                updated_at = NOW()
            RETURNING *
        """), {
            "user_id": normalized_user,
            "connector_type": normalized_connector,
            "status": (status or "incomplete"),
            "non_secret_config": json.dumps(non_secret_config or {}),
            "secret_config": json.dumps(secret_config or {}),
            "validation_error": validation_error,
            "configured_at": now if non_secret_config or secret_config else None,
            "rotated_at": now if secret_config else None,
        })).mappings().first()
    return dict(row)


async def clear_connector_config(
    user_id: str,
    connector_type: str,
) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            DELETE FROM connector_configs
            WHERE user_id = :user_id
              AND connector_type = :connector_type
        """), {
            "user_id": _normalize_user_id(user_id),
            "connector_type": _normalize_connector(connector_type),
        })
    return bool(result.rowcount)


def validate_fundingpips_connector_config(non_secret_config: dict[str, Any], secret_config: dict[str, Any]) -> tuple[str, str | None]:
    healthcheck_url = str(non_secret_config.get("healthcheck_url") or "").strip()
    account_id = str(non_secret_config.get("external_account_id") or "").strip()
    api_token = str(secret_config.get("api_token") or "").strip()
    if not healthcheck_url:
        return ("incomplete", "healthcheck_url is required for FundingPips external sync")
    if not account_id:
        return ("incomplete", "external_account_id is required for FundingPips external sync")
    if not api_token:
        return ("incomplete", "api_token is required for FundingPips external sync")
    return ("configured", None)


async def _perform_fundingpips_external_probe(run: dict[str, Any], config_row: dict[str, Any]) -> dict[str, Any]:
    non_secret = config_row.get("non_secret_config") or {}
    secret = config_row.get("secret_config") or {}
    probe_url = str(non_secret.get("healthcheck_url") or "").strip()
    account_id = str(non_secret.get("external_account_id") or "").strip()
    api_token = str(secret.get("api_token") or "").strip()
    timeout_seconds = float(non_secret.get("timeout_seconds") or DEFAULT_EXTERNAL_HEALTHCHECK_TIMEOUT_SECONDS)
    request_started_at = datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
    if account_id:
        headers["X-Connector-Account"] = account_id

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(probe_url, headers=headers)
        if response.status_code >= 400:
            raise ConnectorSyncError(
                f"FundingPips external probe returned HTTP {response.status_code}",
                code="external_probe_http_error",
                category="upstream",
                transient=response.status_code >= 500,
                status_detail="External FundingPips probe endpoint rejected the request.",
                source_summary={
                    "connector_mode": "external_probe",
                    "probe_url": probe_url,
                    "response_status_code": response.status_code,
                },
                diagnostics={"response_preview": response.text[:200]},
            )
        payload = response.json() if "application/json" in (response.headers.get("content-type") or "") else {}
    except httpx.TimeoutException as exc:
        raise ConnectorSyncError(
            "FundingPips external probe timed out",
            code="external_probe_timeout",
            category="network",
            transient=True,
            status_detail="FundingPips external endpoint timeout during sync.",
            source_summary={"connector_mode": "external_probe", "probe_url": probe_url},
            diagnostics={"exception_type": exc.__class__.__name__, "timeout_seconds": timeout_seconds},
        ) from exc
    except httpx.HTTPError as exc:
        raise ConnectorSyncError(
            f"FundingPips external probe transport error: {exc}",
            code="external_probe_transport_error",
            category="network",
            transient=True,
            status_detail="FundingPips external endpoint request failed.",
            source_summary={"connector_mode": "external_probe", "probe_url": probe_url},
            diagnostics={"exception_type": exc.__class__.__name__},
        ) from exc

    return {
        "result_category": "external_probe",
        "status_detail": "FundingPips external probe completed successfully.",
        "source_summary": {
            "connector_mode": "external_probe",
            "probe_url": probe_url,
            "probed_account_id": account_id,
            "probed_at": request_started_at.isoformat(),
        },
        "diagnostics": {
            "response_status": response.status_code,
            "remote_status": payload.get("status") if isinstance(payload, dict) else None,
            "remote_message": payload.get("message") if isinstance(payload, dict) else None,
        },
    }


async def ingest_account_snapshot(payload: dict[str, Any]) -> bool:
    account = await upsert_trading_account(payload)
    snapshot_time = _parse_dt(payload.get("timestamp")) or datetime.now(timezone.utc)
    account_user_id = _normalize_user_id(account.get("user_id") or payload.get("user_id"))
    connector_type = _normalize_connector(payload.get("connector_type") or account.get("connector_type"))
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
    if account_user_id:
        await upsert_connector_lifecycle(
            user_id=account_user_id,
            connector_type=connector_type,
            status="connected",
            is_connected=True,
            last_sync_at=snapshot_time,
            last_activity_at=snapshot_time,
            metadata={"source": "ingest_account_snapshot"},
        )
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
    account_user_id = _normalize_user_id(account.get("user_id") or payload.get("user_id"))
    connector_type = _normalize_connector(payload.get("connector_type") or account.get("connector_type"))
    account_size = payload.get("account_size") or account.get("account_size") or 10000
    pnl = payload.get("pnl") or 0
    if abs(pnl) > account_size:
        if account_user_id:
            await upsert_connector_lifecycle(
                user_id=account_user_id,
                connector_type=connector_type,
                status="sync_error",
                is_connected=True,
                error="Trade rejected: pnl exceeds account_size",
                last_activity_at=datetime.now(timezone.utc),
                metadata={"source": "ingest_trade"},
            )
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
    if account_user_id:
        now = datetime.now(timezone.utc)
        await upsert_connector_lifecycle(
            user_id=account_user_id,
            connector_type=connector_type,
            status="connected",
            is_connected=True,
            last_activity_at=now,
            metadata={"source": "ingest_trade"},
        )
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
    normalized_user = _normalize_user_id(payload.get("user_id"))
    if normalized_user:
        event_type = str(payload.get("event_type") or "").strip().lower()
        status = "connected"
        error = None
        if event_type in {"sync_error", "error", "degraded"}:
            status = "sync_error" if event_type == "sync_error" else "degraded"
            error = f"Connector event: {event_type}"
        await upsert_connector_lifecycle(
            user_id=normalized_user,
            connector_type=params["connector_type"],
            status=status,
            is_connected=True,
            last_activity_at=params["event_time"],
            error=error,
            metadata={"source": "ingest_event", "event_type": event_type},
        )
