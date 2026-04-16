# Connector Ingestion Architecture

This backend now supports a connector-first ingestion pipeline.

## Canonical domain concepts

- **TradingAccount** (`trading_accounts`): user-linked account identity per connector.
- **AccountSnapshot** (`account_snapshots`): point-in-time account balance/equity/risk values.
- **Position** (`positions`): normalized open-position state.
- **Trade** (`trades`): normalized closed trades (existing analytics/journal table).
- **ConnectorEvent** (`connector_events`): normalized connector lifecycle/events.

## API surface

### Generic connector ingestion

- `POST /ingest/accounts`
- `POST /ingest/account-snapshots`
- `POST /ingest/positions`
- `POST /ingest/trades`
- `POST /ingest/events`
- `POST /ingest/csv/trades` (proof of non-extension ingest path)

### Connector lifecycle management (new)

- `GET /connectors/overview` (bearer-authenticated)
- `GET /connectors/{connector_type}` (bearer-authenticated detail)
- `POST /connectors/{connector_type}/connect` (mark connected, optional lightweight account setup)
- `POST /connectors/{connector_type}/sync` (manual sync signal / timestamp update)
- `POST /connectors/{connector_type}/disconnect` (deactivate connector accounts + mark disconnected)

Lifecycle state is persisted in `connector_lifecycle` with:

- `status`: `connected` | `degraded` | `sync_error` | `disconnected`
- `is_connected`: boolean connectivity flag for UX clarity
- `last_sync_at`, `last_activity_at`
- `last_error`, `last_error_at`
- `metadata` for action/event provenance

Account membership remains represented by `trading_accounts` rows grouped by `connector_type`.

### Legacy compatibility

- `/extension/data` and `/extension/trade` are still supported.
- They now internally feed the same normalized ingestion layer.

## Migration strategy

- Incremental pivot: keep existing dashboard + Telegram flows using `trades` and live memory state.
- Add canonical connector tables without destructive changes.
- New connectors can be added without imitating the FundingPips extension payload shape.

## Phase 2 hardening (PR #36 follow-up)

- **Deterministic account dedup:** `trading_accounts` now uses a computed `account_key` with a unique index to avoid NULL `user_id` uniqueness edge-cases.
- **Stale position cleanup:** open positions now track `position_key`, `is_active`, `last_seen_at`, and `closed_at`; legacy extension ingestion deactivates missing positions only when a snapshot explicitly indicates no open positions.
- **Snapshot growth guard:** account snapshots use short-window dedupe (`SNAPSHOT_DEDUPE_WINDOW_SECONDS`) and skip inserts when account state is unchanged.
- **Richer trade normalization:** canonical trade ingest now preserves `connector_type`, `open_time`, `fees`, `tags`, `source_metadata`, and `import_provenance` (JSON fields) while keeping legacy readers intact.

## Phase 2.5 blocker fixes

- **Startup ordering safety:** `ensure_connector_tables()` now performs table/column/index creation before any dedup/rewire statements that reference those tables.
- **Position identity consistency:** removed legacy `(trading_account_id, symbol, side)` uniqueness assumption and standardized on `position_key` as the canonical conflict identity.

## Current scope vs future work

What exists now:
- Practical connect/sync/disconnect actions for authenticated users.
- Connector status and timestamps shown as first-class fields in connector overview/detail responses.
- Automatic lifecycle updates when ingest snapshots/trades/events flow through canonical ingestion.

Future work (not in this minimal issue scope):
- Background sync workers and queue-backed connector jobs.
- Connector-specific OAuth/API credential handshakes.
- Retry/backoff policy and richer incident timeline per connector.
