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
