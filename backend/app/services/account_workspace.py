from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.database import engine
from app.services.connector_ingest import compute_account_key

DEFAULT_CONNECTION_STATUS = "disconnected"
DEFAULT_SYNC_STATE = "idle"
SYNC_STATE_MAP = {
    "queued": "queued",
    "running": "running",
    "retrying": "retrying",
    "failed": "failed",
    "succeeded": "succeeded",
}
CONNECTION_STATUS_MAP = {
    "connected": "connected",
    "degraded": "degraded",
    "disconnected": "disconnected",
    "sync_error": "sync_error",
    "awaiting_alerts": "awaiting_alerts",
    "active": "active",
    "bridge_required": "bridge_required",
    "waiting_for_registration": "waiting_for_registration",
    "ready_for_account_attach": "ready_for_account_attach",
    "beta_pending": "beta_pending",
    "metadata_saved": "metadata_saved",
    "awaiting_secure_auth": "awaiting_secure_auth",
    "waiting_for_secure_auth_support": "waiting_for_secure_auth_support",
}


def _normalize_connector_type(value: str | None) -> str:
    return (value or "manual").strip().lower().replace("-", "_")


def _normalize_broker_family(broker_name: str | None, connector_type: str | None) -> str:
    broker = str(broker_name or "").strip().lower()
    connector = _normalize_connector_type(connector_type)
    if connector == "mt5_bridge":
        return "mt5"
    if "fundingpips" in broker or connector == "fundingpips_extension":
        return "fundingpips"
    if broker:
        return broker.replace(" ", "_")
    if connector:
        return connector
    return "unknown"


def _normalize_connection_status(raw_status: str | None, is_connected: bool | None) -> str:
    status = str(raw_status or "").strip().lower()
    if status in CONNECTION_STATUS_MAP:
        return status
    if status in {"sync_running", "sync_queued", "sync_retrying"}:
        return "degraded"
    if is_connected is True:
        return "connected"
    return DEFAULT_CONNECTION_STATUS


def _normalize_sync_state(sync_status: str | None) -> str:
    return SYNC_STATE_MAP.get(str(sync_status or "").strip().lower(), DEFAULT_SYNC_STATE)


def _normalize_workspace_row(row: dict[str, Any], *, fallback_user_id: str) -> dict[str, Any]:
    connector_type = _normalize_connector_type(row.get("connector_type"))
    external_account_id = row.get("external_account_id")
    user_id = str(row.get("user_id") or fallback_user_id).strip()
    account_key = row.get("account_key") or compute_account_key(connector_type, user_id, external_account_id)
    display_label = row.get("display_label") or external_account_id

    connector_connection_status = _normalize_connection_status(row.get("lifecycle_status"), row.get("lifecycle_is_connected"))
    connector_sync_state = _normalize_sync_state(row.get("latest_sync_status"))

    # NOTE: lifecycle/sync joins are currently connector-scoped (user_id + connector_type),
    # so these status fields are connector rollups and not per-account guarantees.
    return {
        "account_key": account_key,
        "trading_account_id": row.get("trading_account_id"),
        "user_id": user_id,
        "external_account_id": external_account_id,
        "display_label": display_label,
        "broker_name": row.get("broker_name"),
        "broker_family": _normalize_broker_family(row.get("broker_name"), connector_type),
        "connector_type": connector_type,
        "connection_status": connector_connection_status,
        "sync_state": connector_sync_state,
        "connector_connection_status": connector_connection_status,
        "connector_sync_state": connector_sync_state,
        "status_scope": "connector_rollup",
        "bridge_status": row.get("bridge_status"),
        "bridge_profile": row.get("bridge_profile"),
        "trusted_bridge_id": row.get("trusted_bridge_id"),
        "trusted_bridge_display_name": row.get("trusted_bridge_display_name"),
        "trusted_bridge_last_heartbeat_at": row.get("trusted_bridge_last_heartbeat_at"),
        "bridge_last_sync_at": row.get("bridge_last_sync_at"),
        "account_type": row.get("account_type"),
        "account_size": row.get("account_size"),
        "last_activity_at": row.get("last_activity_at"),
        "last_sync_at": row.get("last_sync_at"),
        "is_primary": bool(row.get("is_primary")),
        "provider_state": (row.get("metadata") or {}).get("provider_state"),
    }


async def list_account_workspaces(telegram_user_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            WITH latest_snapshots AS (
                SELECT trading_account_id, MAX(snapshot_time) AS last_snapshot_at
                FROM account_snapshots
                GROUP BY trading_account_id
            ),
            latest_trades AS (
                SELECT trading_account_id, MAX(COALESCE(close_time, closed_at, logged_at)) AS last_trade_at
                FROM trades
                WHERE trading_account_id IS NOT NULL
                GROUP BY trading_account_id
            ),
            latest_events AS (
                SELECT trading_account_id, MAX(event_time) AS last_event_at
                FROM connector_events
                WHERE trading_account_id IS NOT NULL
                GROUP BY trading_account_id
            ),
            latest_sync_runs AS (
                SELECT DISTINCT ON (connector_type)
                    connector_type,
                    status
                FROM connector_sync_runs
                WHERE user_id = :uid
                ORDER BY connector_type, created_at DESC
            ),
            latest_mt5_bridge AS (
                SELECT DISTINCT ON (user_id)
                    user_id,
                    bridge_id,
                    display_name,
                    last_heartbeat_at
                FROM mt5_trusted_bridges
                WHERE user_id = :uid
                ORDER BY user_id, updated_at DESC
            ),
            canonical_accounts AS (
                SELECT
                    ta.account_key,
                    ta.id AS trading_account_id,
                    ta.user_id,
                    ta.external_account_id,
                    ta.display_label,
                    COALESCE(NULLIF(ta.broker_name, ''), ta.connector_type) AS broker_name,
                    ta.connector_type,
                    ta.account_type,
                    ta.account_size,
                    ta.metadata,
                    GREATEST(
                        COALESCE(ls.last_snapshot_at, 'epoch'::timestamptz),
                        COALESCE(lt.last_trade_at, 'epoch'::timestamptz),
                        COALESCE(le.last_event_at, 'epoch'::timestamptz),
                        ta.updated_at
                    ) AS last_activity_at,
                    ls.last_snapshot_at AS last_sync_at,
                    FALSE AS is_primary
                FROM trading_accounts ta
                LEFT JOIN latest_snapshots ls ON ls.trading_account_id = ta.id
                LEFT JOIN latest_trades lt ON lt.trading_account_id = ta.id
                LEFT JOIN latest_events le ON le.trading_account_id = ta.id
                WHERE ta.user_id = :uid AND ta.is_active = TRUE
            ),
            legacy_only_accounts AS (
                SELECT
                    NULL::TEXT AS account_key,
                    NULL::INTEGER AS trading_account_id,
                    pa.telegram_user_id AS user_id,
                    pa.account_id AS external_account_id,
                    pa.label AS display_label,
                    COALESCE(NULLIF(pa.broker, ''), 'fundingpips') AS broker_name,
                    'fundingpips_extension'::TEXT AS connector_type,
                    pa.account_type AS account_type,
                    pa.account_size AS account_size,
                    '{}'::jsonb AS metadata,
                    pa.created_at AS last_activity_at,
                    NULL::TIMESTAMPTZ AS last_sync_at,
                    FALSE AS is_primary
                FROM prop_accounts pa
                WHERE pa.telegram_user_id = :uid
                  AND pa.is_active = TRUE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM canonical_accounts ca
                      WHERE ca.external_account_id = pa.account_id
                  )
            ),
            merged_accounts AS (
                SELECT * FROM canonical_accounts
                UNION ALL
                SELECT * FROM legacy_only_accounts
            )
            SELECT
                ma.*,
                lc.status AS lifecycle_status,
                lc.is_connected AS lifecycle_is_connected,
                COALESCE(ma.last_sync_at, lc.last_sync_at) AS last_sync_at,
                lsr.status AS latest_sync_status,
                mba.bridge_status AS bridge_status,
                mba.bridge_url AS bridge_profile,
                mba.last_bridge_sync_at AS bridge_last_sync_at,
                lmb.bridge_id AS trusted_bridge_id,
                lmb.display_name AS trusted_bridge_display_name,
                lmb.last_heartbeat_at AS trusted_bridge_last_heartbeat_at
            FROM merged_accounts ma
            LEFT JOIN connector_lifecycle lc
              ON lc.user_id = :uid AND lc.connector_type = ma.connector_type
            LEFT JOIN latest_sync_runs lsr
              ON lsr.connector_type = ma.connector_type
            LEFT JOIN mt5_bridge_accounts mba
              ON mba.user_id = :uid AND mba.trading_account_id = ma.trading_account_id
            LEFT JOIN latest_mt5_bridge lmb
              ON lmb.user_id = :uid AND ma.connector_type = 'mt5_bridge'
            ORDER BY ma.last_activity_at DESC NULLS LAST, ma.external_account_id
        """), {"uid": telegram_user_id})
        rows = [dict(row) for row in result.mappings().all()]
    return [_normalize_workspace_row(row, fallback_user_id=telegram_user_id) for row in rows]


async def get_account_workspace(telegram_user_id: str, account_key: str) -> dict[str, Any] | None:
    workspaces = await list_account_workspaces(telegram_user_id)
    for workspace in workspaces:
        if workspace["account_key"] == account_key:
            return workspace
    return None
