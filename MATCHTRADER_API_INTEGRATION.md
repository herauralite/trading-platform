# Match-Trader Trader API ingestion (server-side, read-only)

Status: implemented on branch `feature/matchtrader-api-ingestion`, **disabled by default, shadow mode only**.
Nothing here is deployed. Written 2026-11-14.

Target: `Match-Trader Trader API -> server-side worker -> PostgreSQL (mtapi_* tables)`, so an account keeps
syncing with the browser closed. This branch stops at the shadow import. Feeding the dashboard, journal,
analytics and alerts from it (the "canonical cutover") is deliberately not built yet - see "Not built".

## Contract snapshot (as supplied 2026-11-14, NOT re-verified by the implementer)

The provider documentation at app.theneo.io could not be opened from the build environment, so the code
follows the contract below exactly as written in the research brief. Re-check it against the live docs and
run the smoke script before trusting any field.

- Base URL `https://trader-api.match-trader.com`, `Authorization: Bearer <JWT>`. Token is created by the user
  in Match-Trader "API Access", scoped to one trading account, shown once, expires within a year, can be
  revoked. No refresh endpoint documented: replacement is manual.
- GET endpoints used (documented per-endpoint limit per account per minute):
  `/v1/platform-details` 10, `/v1/account` 10, `/v1/account-summary` 20, `/v1/positions` 20,
  `/v1/pending-orders` 20, `/v1/closed-positions` 20 (`from`, `to`, `limit` <= 500), `/v1/ledgers` 20.
- History is newest-first, capped at `limit`, no cursor. Times are UTC epoch milliseconds.
- Rate headers `X-RateLimit-Remaining-Endpoint`, `X-RateLimit-Remaining-Global`; 429 carries `Retry-After`.
- Errors are problem-details; code branches on HTTP status and `type` only.
- Market data, trading (mutating) endpoints and MCP are out of scope and not callable from this code.

## What is implemented

| Area | File | Notes |
| --- | --- | --- |
| GET-only client | `backend/app/services/matchtrader_api/client.py` | Fixed host, 7 allowlisted paths, no redirects, TLS on, timeouts, 5 MB cap, typed errors, token never in repr/logs |
| Schemas | `schemas.py` | Decimal money, string ids, ms timestamps, null/unknown enums preserved, raw kept |
| Token encryption | `crypto.py` | AES-256-GCM, versioned keys, AAD binds user + connection + external identity |
| Rate budget | `ratelimit.py` | Per account, per endpoint sliding window at 70% of documented limit; backfill limited to half; honours headers and Retry-After |
| History planner | `backfill.py` | Bisect on a full page; per-millisecond queries only after proving inclusive bounds; otherwise explicit gap |
| Persistence | `store.py` | 9 new `mtapi_*` tables; every write fenced by status + generation + lease |
| Sync pass | `engine.py` | Live (summary, positions, pending orders) and history (closed positions, ledgers), independent of each other |
| Worker | `worker.py` | Polls due connections; one failing account never blocks others |
| Routes | `routes.py` | `/connectors/matchtrader-api/connections...` - session auth only, owner from session |
| Reconciliation | `reconcile.py` | Read-only comparison with legacy extension trades; proposes, never merges |
| Smoke test | `backend/scripts/matchtrader_api_smoke.py` | Real API, GET-only, hidden token prompt, prints shapes not values |

## Routes (all require a TaliTrade session; extension tokens are refused)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/connectors/matchtrader-api/connections` | Body `{token}`. Validates with read calls, binds identity, stores encrypted token |
| GET | `/connectors/matchtrader-api/connections` | Status, per-resource freshness, history coverage. Token is masked (`...abcd`) |
| POST | `.../{id}/rotate` | New token must resolve to the same external account |
| POST | `.../{id}/pause`, `.../{id}/resume` | |
| POST | `.../{id}/sync` | Manual pass; shares the lease and rate budget with the worker |
| POST | `.../{id}/backfill` | Body `{from_ms}`. Queues history import from that date |
| GET | `.../{id}/reconciliation` | Read-only report vs legacy trades (only if the user owns the linked account) |
| DELETE | `.../{id}` | Stops sync, deletes stored token, keeps imported history. Does not revoke the token at the broker |

A connection the caller does not own always answers `404 Connection not found`.

## Identity and ownership

- External identity = (`platformUrl` host, `login`). `brokerName` is display only. The platform host is never
  contacted; it is only a namespace.
- One live connection per external identity (database unique index). A second attempt gets a generic 409.
- If `platformUrl` is missing the connection is created as `pending_review` and is never synced or linked.
- The connection is linked to an existing TaliTrade account only when the signed-in user already owns the
  account id equal to the login. A broker token never transfers ownership of existing or orphaned records.
- Connection statuses: `active`, `paused`, `needs_reauth` (401), `forbidden` (required read not permitted),
  `pending_review`, `error`, `disconnected`.

## Field mapping (shadow tables)

| API | Stored as |
| --- | --- |
| account-summary `balance, credit, equity, margin, freeFunds, marginLevel, profit` | `mtapi_account_summary` (`freeFunds` -> `free_funds`) |
| positions `id` | `mtapi_positions.external_id` (key: connection + id) |
| pending-orders `id` | `mtapi_pending_orders.external_id` |
| closed-positions `uid` | `mtapi_closed_positions.uid` (key: connection + uid); `id` -> `position_id` (groups partial closes); `closingOrderId` kept |
| ledgers `id` | `mtapi_ledgers.external_id` (separate table; never added to trade P&L) |
| account `email, firstName, lastName` | not stored |

`netProfit` is the after-fees figure; `profit` is before commission and swap. Nothing adds commission or swap
on top of `netProfit`, and `CLOSE_POSITION` ledger rows are not summed with closed positions.

## Database changes

Additive only: 9 tables prefixed `mtapi_` plus indexes, created at startup **only when
`TALITRADE_MT_API_ENABLED=true`**. No existing table is altered. To remove: drop the `mtapi_*` tables
(order is in `store.DROP_ORDER`); nothing else references them.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TALITRADE_MT_API_ENABLED` | off | Routes + table creation |
| `TALITRADE_MT_API_WORKER_ENABLED` | off | Background polling (needs the flag above too) |
| `TALITRADE_MT_TOKEN_KEYS` | - | JSON `{"v1": "<base64 of 32 random bytes>"}`. Secret. Separate from `SECRET_KEY` |
| `TALITRADE_MT_TOKEN_KEY_ACTIVE` | - | Key version used for new tokens, e.g. `v1` |
| `TALITRADE_MT_API_LIVE_INTERVAL_SECONDS` | 30 | Summary/positions/pending orders (min 10) |
| `TALITRADE_MT_API_HISTORY_INTERVAL_SECONDS` | 120 | Closed positions/ledgers (min 30) |
| `TALITRADE_MT_API_HISTORY_OVERLAP_SECONDS` | 900 | Re-read window on each history pass |
| `TALITRADE_MT_API_RECONCILE_WINDOW_SECONDS` / `_EVERY_SECONDS` | 48 h / 6 h | Periodic wide re-check for late corrections |
| `TALITRADE_MT_API_BACKFILL_INTERVALS_PER_CYCLE` | 2 | Backfill work per pass, after live work |

Generate a key: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
Key rotation: add `v2` to the JSON, set it active; old tokens still decrypt with `v1` until rotated.

At defaults one account uses about 6 live + 1-3 history requests per minute.

## Tests

- `backend/tests/test_matchtrader_api_unit.py` - no database needed.
- `backend/tests/test_matchtrader_api_db.py` - needs `TALITRADE_TEST_DATABASE_URL` pointing at a disposable
  PostgreSQL whose name contains `test` or `disposable`; skipped otherwise. It drops and recreates `mtapi_*`
  and a throwaway `trades` table.

## Staging steps (nothing below has been done)

1. Create a separate staging database and service; set the two flags and the token keys there only.
2. Run the smoke script with a DEMO read-only token. Compare field names with the contract above.
3. Connect the demo account through the API, start a backfill, watch `GET .../connections` coverage.
4. Close the browser; confirm freshness timestamps keep advancing.
5. Compare `GET .../reconciliation` with what the extension recorded for the same account.

## Rollout order and gates

local tests -> staging -> broker confirms API access and hosted polling are allowed -> one opt-in account in
shadow -> coverage has no gaps, counts and P&L reconcile, partial closes look right -> build and review the
canonical cutover -> expand.

## Rollback

Set `TALITRADE_MT_API_WORKER_ENABLED=false` (polling stops within one loop) and, if needed,
`TALITRADE_MT_API_ENABLED=false` (routes 404). Imported rows, checkpoints and stored tokens stay in place.
Because this is shadow-only, no dashboard, journal or alert reads these tables, so there is nothing to
re-point. Do not drop tables as part of a rollback. Users revoke their own tokens in Match-Trader.

## Not built (needs a decision before work continues)

- Canonical cutover: writing API data into `trades` / `positions` / `account_snapshots`, per-account source
  precedence, rejecting extension writes for cut-over accounts.
- Connection UI in the dashboard (token form, status, reconnect guidance).
- SSE / alert publishing from API data (the repo has no SSE today), alert de-duplication outbox.
- Token expiry tracking: no trustworthy source documented, so nothing is stored or inferred from the JWT.
- Multi-replica rate budgeting (in-process today; the app enforces a single process).
- Symbols, quotes, candles, trading hours.

## Known data limits

No historical equity curve, MAE/MFE, tick data, order/deal history, original stop levels, or prop-rule
state. A close time with 500+ entries in a single millisecond cannot be fully retrieved and is reported as a
gap. Corrections older than the reconcile window are not detected. Ledger rows cannot be joined to trades.

## Open broker questions (block real-account use, not development)

1. Is this API enabled for the exact broker and account types (evaluation, funded)?
2. May a third-party service hold a read-only token and poll from a datacenter IP? Is an IP allowlist needed?
3. What are the account's global rate limits?
4. How far back does history go, and are `from`/`to` inclusive?
5. Is (`platformUrl` host, `login`) stable across resets and stage changes? Is there a better account id?
6. Are webhooks, streaming or deal-level history available?

Note: the brief says "FundedPips"; earlier project material says "FundingPips". Confirm which firm is meant.
