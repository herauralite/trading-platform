# MT5 Connector Phase 1 Foundation

## Broker adapter model fit

TaliTrade treats MT5 as a **broker/platform connector** (`connector_type = mt5_bridge`) alongside existing connector types, rather than mixing it with public market-data providers.

This slice adds MT5 to the same connector catalog + connector config surface used by other account connectors, while keeping connector behavior additive and backward compatible.

## Bridge-based connectivity contract

MT5 access is modeled as a backend bridge contract in `app/services/mt5_bridge.py` with explicit interfaces for:

- account summary
- balances/equity
- open positions
- orders
- trade history

Phase 1 ships a safe stub client (`StubMT5BridgeClient`) so the API can expose truthful "bridge required" semantics without faking live connectivity.

## Why this avoids cornering the architecture

- No auth/session changes.
- No API-base/frontend host resolution changes.
- Additive schema only (`mt5_bridge_accounts` table).
- MT5-specific bridge details are kept behind backend service boundaries, not spread throughout UI routes.
- Existing FundingPips/manual/CSV connector flows continue unmodified.
