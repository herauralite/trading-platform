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
