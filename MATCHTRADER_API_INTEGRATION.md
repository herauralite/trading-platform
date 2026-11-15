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
| Persistence | `store.py` | 10 new `mtapi_*` tables; every write fenced by status + generation + lease |
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
| POST | `.../{id}/sync` | Queues a refresh (202). The worker runs it under the lease and rate budget |
| GET | `.../{id}/sync` | State of that request: queued, running, completed, partial, failed, cancelled |
| POST | `.../{id}/backfill` | Body `{from_ms}`. Queues history import from that date |
| GET | `.../{id}/reconciliation` | Read-only report vs legacy trades (only if the user owns the linked account) |
| DELETE | `.../{id}` | Stops sync, deletes stored token, keeps imported history. Does not revoke the token at the broker |

A connection the caller does not own always answers `404 Connection not found`.

## Dashboard integration (branch `feature/matchtrader-dashboard`)

The served dashboard is `frontend/public/app.html` (Vite copies `public/` to `dist/`; `vercel.json` rewrites
`/app` to `/app.html`). `frontend/app.html` and the React app under `frontend/src/` are not what `/app` serves
and were not changed.

| Piece | File |
| --- | --- |
| Owner-scoped read models, sync verdict | `backend/app/services/matchtrader_api/read_models.py` |
| `GET .../{id}/snapshot`, `GET .../{id}/trades?limit&cursor` | `routes.py` |
| HTTP client + pure display logic | `frontend/public/tali-matchtrader-api.js` |
| Provider bridge (cards, snapshot -> cards, history, sync, token form) | `frontend/public/tali-matchtrader-dashboard.js` |
| Dispatch hooks, token form, account sheet | `frontend/public/app.html` |

Flow: token form -> `POST /connections` (stores the connection and queues the first refresh) ->
`POST /{id}/backfill` (last 7 days) -> the page follows the queued refresh with `GET /{id}/sync` every 1.5 s
and re-reads the snapshot when it reaches a final state -> the worker finishes the import while the page
re-reads the snapshot every 10 s. No browser request ever waits on the broker: every web request reads or
writes PostgreSQL only. The page's 5-second status timer reads the snapshot at most every 8 s.

### Refresh requests ("Sync now")

`POST /{id}/sync` answers `202 {"accepted": true, "already_pending": bool, "request": {...}}` at once. It only
queues work. One row per connection in `mtapi_sync_requests` is the durable request; asking again while it is
queued or running returns the same request, so there is never a second job. The worker claims it under the
normal lease (so it is fenced by generation like every other write), runs the three live calls, and records
the result. `GET /{id}/sync` and the snapshot's `sync_request` report it:

| `request.state` | Meaning | Dashboard label |
| --- | --- | --- |
| `queued` | Stored, waiting for the worker | queued |
| `running` | The lease holder is calling the broker | running |
| `completed` | Balance, positions and pending orders were all stored | completed |
| `partial` | Balance and positions stored; `resources` says what was not | partial ("Partially updated") |
| `failed` | Live data was not refreshed; `detail` says why (`unauthorized`, `rate_limited`, `failed`, ...) | failed |
| `cancelled` | The connection was paused, disconnected or its token replaced first | failed |

A request left `running` by a crashed pass is picked up by the next lease holder, at most 3 times. With the
worker off, `POST /{id}/sync` answers 503 and queues nothing. A 202 never means "synchronized".

### Keeping live data fresh during imports

* Rows are written in bulk: one `INSERT .. SELECT FROM jsonb_to_recordset(..) ON CONFLICT` per 500 rows
  instead of one round trip per row. Same per-row rules (uid de-duplication, revision and `previous_raw` only
  move when the payload changed), same single transaction per history range.
* A worker round does all due live work (and queued refreshes) first, then history.
* History is bounded per pass by range count and by `TALITRADE_MT_API_HISTORY_PASS_SECONDS` (12). Between
  ranges the pass refreshes live data if it has fallen due. Ranges not reached stay queued.
* While ranges are queued, history runs again after `TALITRADE_MT_API_HISTORY_BACKLOG_INTERVAL_SECONDS` (20)
  instead of the 120 s history interval. A new backfill starts at the next round.
* Backfill that runs out of rate budget stops quietly and continues next pass (it is not a failure).
* The worker sleeps until the next scheduled work (1-6 s) rather than a fixed 5-7 s.
* Each pass logs timings only: `matchtrader pass connection=.. seconds=.. live_seconds=.. history_seconds=..`.

Measured in the sandbox (simulated broker at 400 ms per call, PostgreSQL behind a 60 ms round-trip link,
1,200 closed entries + 150 ledger rows over 7 days, default intervals; not a real broker or the real network):

| | Before (`7fa9e30`) | After |
| --- | --- | --- |
| First refresh after connect, until a result is reported | 48.6 s inside one web request | 5.4 s, queued; the web request returns at once |
| Database round trips for that first refresh | 695 | 63 |
| Longest history transaction (500 rows) | 33.5 s | 0.9 s |
| Longest worker pass | 52.8 s | 10.6 s |
| Live refreshes while importing | 2 in 112 s, worst gap 84 s | 3 in 123 s, worst gap 37 s (typical 32 s) |
| 7-day import progress after 2 minutes | 500 of 1,200 | 1,100 of 1,200 |

Stale thresholds were not changed.

Snapshot rules: money is a decimal string, times are UTC ISO-8601, unknown is `null`. `summary`, `positions`
and `pending_orders` are `null` until their first successful fetch (`[]` means fetched and empty).
`freshness.<resource>.status` is `fresh | degraded | stale | failed | never_synced | not_permitted`.
`history.period_stats` is a SQL aggregate over every imported closed entry, not over a page.

Example (synthetic values):

```json
{"connection": {"id": 12, "account_key": "mtapi:12", "connector_type": "matchtrader_api", "broker_name": "Example Broker",
                "display_label": "Example Broker · …0001", "masked_login": "…0001", "currency": "USD",
                "provider_account_type": "DEMO", "status": "active", "display_state": "ready"},
 "summary": {"balance": "50123.45", "equity": "50100", "free_funds": "50100", "margin": "0", "profit": "-23.45",
             "credit": "0", "margin_level": null, "fetched_at": "2026-01-01T12:00:00Z"},
 "positions": [], "pending_orders": [],
 "freshness": {"summary": {"status": "fresh", "last_success_at": "2026-01-01T12:00:00Z", "last_error": null, "is_stale": false}},
 "history": {"coverage_status": "in_progress", "closed_entry_count": 3,
             "period_stats": {"net_profit": "31.25", "closed_entries": 3, "winning_entries": 2, "losing_entries": 1}},
 "background_refresh": {"enabled": true, "live_interval_seconds": 30, "history_interval_seconds": 120}}
```

What API accounts show: balance, equity, floating P&L, open positions, pending orders, sync freshness, recent
closed trades, and imported-period net P&L / winning-entry ratio. Shown as unavailable (never guessed): daily
P&L, payout and challenge progress, risk rules, account size, prop stage, equity chart, journal notes.
Currency precision is not stored; amounts display with 2 decimals.

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
| `TALITRADE_MT_API_GLOBAL_PER_MINUTE` | 30 | Account-wide request cap across all endpoints (the real ceiling is unpublished) |
| `TALITRADE_MT_API_CONNECT_ENABLED` | off | Allows NEW connections and shows the token form. Needs the three above |
| `TALITRADE_MT_API_HISTORY_PASS_SECONDS` | 12 | Time budget for history in one pass (min 2) |
| `TALITRADE_MT_API_HISTORY_BACKLOG_INTERVAL_SECONDS` | 20 | History interval while ranges are still queued (min 5) |
| `TALITRADE_MT_API_LIVE_INTERVAL_SECONDS` | 30 | Summary/positions/pending orders (min 10) |
| `TALITRADE_MT_API_HISTORY_INTERVAL_SECONDS` | 120 | Closed positions/ledgers (min 30) |
| `TALITRADE_MT_API_HISTORY_OVERLAP_SECONDS` | 900 | Re-read window on each history pass |
| `TALITRADE_MT_API_RECONCILE_WINDOW_SECONDS` / `_EVERY_SECONDS` | 48 h / 6 h | Periodic wide re-check for late corrections |
| `TALITRADE_MT_API_BACKFILL_INTERVALS_PER_CYCLE` | 2 | Backfill work per pass, after live work |

Generate a key: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
Key rotation: add `v2` to the JSON, set it active; old tokens still decrypt with `v1` until rotated.

At defaults one account uses about 6 live + 1-3 history requests per minute. A 429 pauses only that endpoint when the
rate headers prove the account still has budget; otherwise the whole account is paused until `Retry-After`.

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

## Coordinated rollout (nothing here has been done; no production flag was changed)

Five gates, opened in this order. Each can be verified before the next, and each has its own way back.

| # | Gate | How it is opened | Ready when | Roll back |
| --- | --- | --- | --- | --- |
| 1 | Secrets | Set `TALITRADE_MT_TOKEN_KEYS` (new random 32-byte key, base64, JSON by version) and `TALITRADE_MT_TOKEN_KEY_ACTIVE` on the backend. Not `SECRET_KEY`, not reused from local testing | Value present in the deployment's secret store and backed up | Remove them (only before gate 5: afterwards stored tokens need the key) |
| 2 | Schema + authenticated routes | `TALITRADE_MT_API_ENABLED=true`. Startup creates the ten `mtapi_*` tables (`CREATE TABLE IF NOT EXISTS`, additive, nothing existing is altered). Routes start answering | `GET /connectors/matchtrader-api/connections` is 401 without a session and, with one, returns `"connections": []` and `service.accepting_connections: false`; `POST .../connections` is 503 | Flag off: routes answer 404. Tables stay, unused |
| 3 | Worker | `TALITRADE_MT_API_WORKER_ENABLED=true` (single backend process: `WEB_CONCURRENCY=1`) | Log line `Match-Trader API worker started`; `service.worker: true` | Flag off: polling stops, stored data stays readable |
| 4 | Isolation and sign-in checks | No flag. Two real Telegram users, one test connection (see below) | Checks pass | - |
| 5 | UI availability | `TALITRADE_MT_API_CONNECT_ENABLED=true`, with the frontend build that contains this branch already deployed | `service.accepting_connections: true`; the FundingPips card opens the token form | Flag off: the form closes at once; connected accounts keep syncing and stay visible |

The dashboard offers the token form only when the server reports `accepting_connections` (secure storage +
worker + connect gate). `POST /connections` enforces the same thing, so a stale page cannot create a
connection either: a token submitted early is refused before it is used or stored.

**FundingPips card while the form is closed.** On this branch the FundingPips card no longer falls back to
the old FundingPips password login. Until gate 5 is open (or with the flag off, or on any server without the
worker) the card is disabled and reads "API connection is unavailable on this server". So deploying this
frontend before gate 5 means nobody can add a FundingPips account for that period. Accounts already linked
through the extension are not affected. Deploy backend gates 1-3 first, then frontend and gate 5 together,
or accept that window knowingly.

**Rollback implications.**
* Closing gate 5 only stops new connections. It does not stop syncing.
* Turning the worker off freezes data at the last sync; the dashboard shows it as delayed/stale, not fresh.
* Turning the API flag off hides everything (404). Nothing is deleted; turning it back on restores it.
* Rolling the frontend back restores the previous FundingPips password path; API accounts disappear from that
  page but remain stored.
* Never remove or replace the active token key while connections exist: stored tokens become unreadable and
  every user must reconnect. Rotate by adding a new version and changing `..._KEY_ACTIVE`.
* The schema is additive. Dropping `mtapi_*` tables is the only destructive step and is not part of any rollback.

**Still to be checked by people, not by the automated tests.**
* Real Telegram sign-in on a deployed (non-localhost) host, then connecting with a real read-only token.
* Two real Telegram users: B cannot see or operate A's connection (the automated two-user tests use signed
  test sessions against a real PostgreSQL, not Telegram).
* A real broker: first refresh time, live refresh interval during a real 7-day import (read the
  `matchtrader pass` log lines), and behaviour on a real 429.
