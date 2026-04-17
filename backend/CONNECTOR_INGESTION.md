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

### Connector lifecycle management

- `GET /connectors/overview` (bearer-authenticated)
- `GET /connectors/{connector_type}` (bearer-authenticated detail)
- `POST /connectors/{connector_type}/connect` (mark connected, optional lightweight account setup)
- `POST /connectors/{connector_type}/sync` (queue async sync run)
- `GET /connectors/{connector_type}/sync-runs?limit=10` (recent run history)
- `POST /connectors/{connector_type}/disconnect` (deactivate connector accounts + mark disconnected)

`/connectors/{connector_type}/sync` only accepts connectors where catalog metadata marks `supports_live_sync=true`. Unsupported types (for example `manual` and `csv_import`) are rejected with a clear 4xx and no run enqueue.

Lifecycle state is persisted in `connector_lifecycle` with:

- `status`: `connected` | `degraded` | `sync_error` | `disconnected` | `sync_queued` | `sync_running` | `sync_retrying`
- `is_connected`: boolean connectivity flag for UX clarity
- `last_sync_at`, `last_activity_at`
- `last_error`, `last_error_at`
- `metadata` for action/event provenance

Account membership remains represented by `trading_accounts` rows grouped by `connector_type`.

## Sync execution model (Issue #54)

Sync execution is persisted in `connector_sync_runs` and executed by a durable database-claim worker loop.

`connector_sync_runs` fields:
- `status`: `queued` → `running` → `succeeded` / `failed`, with `retrying` between attempts.
- `created_at`, `started_at`, `finished_at`.
- `retry_count`, `max_retries`, `next_retry_at`.
- `error_detail`, `result_detail`, `metadata`.

Durable claim/lease behavior:
- Manual sync enqueue creates a run in `queued` (DB is source of truth).
- Worker claims runs with `FOR UPDATE SKIP LOCKED` and writes a lease (`lease_owner`, `lease_expires_at`).
- Due `retrying` runs (`next_retry_at <= NOW()`) are claimable without in-memory timers.
- Stale `running` runs whose lease expired are reclaimable after restart/process loss.

Retry behavior:
- Failures set run state to `retrying` and persist `next_retry_at` + incremented `retry_count`.
- No in-memory `sleep` controls run timing; workers poll DB for due work.
- If retries are exhausted, run is marked `failed` and lifecycle moves to `sync_error`.

Lifecycle integration:
- Queue/run/retry transitions push lifecycle into `sync_queued`, `sync_running`, `sync_retrying`.
- Success moves lifecycle back to `connected` and updates `last_sync_at`.
- Terminal failure sets lifecycle `sync_error` and `last_error`.

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
- Persistent sync run history and asynchronous manual sync orchestration.
- Retry/backoff and per-run error visibility in connector surfaces.

Restart behavior:
- If app stops mid-run, the run remains `running` with a lease timestamp.
- On restart (or on another process), expired leases are reclaimed and re-executed safely.
- Queued and retrying work survives process restarts because scheduling data is persisted.

## Operational concurrency guardrails

- The app currently keeps `WEB_CONCURRENCY=1` enforced at startup.
- Reason: legacy non-sync schedulers (news/weekend/daily-summary) still run from web-process lifespan and are not yet isolated with leader election.
- Durable connector sync claim/lease logic remains in place, but global multi-process web execution is intentionally blocked until non-sync schedulers are split or leader-gated.

Future work (not in this issue scope):
- External worker deployment topology/metrics dashboards.
- Lease heartbeats for very long-running connector sync implementations.
- Cancellation controls, dead-letter handling, and richer incident analytics.
