# TaliTrade Repository Guide

TaliTrade is a premium SaaS trading platform for prop firm traders. It combines live account ingestion, analytics, Telegram alerting, and a web dashboard so traders can run their desk from one place.

## Source of truth
- `TALITRADE_CONTEXT.md` is the current-state operational source of truth for deployment, auth flow, endpoints, and launch status.
- The original 25-file TaliTrade model is the intended blueprint, not an assumption that every detail is already implemented in this repo.

## Current architecture
- **Backend (`backend/`)**: FastAPI API, Telegram webhook/bot logic, connector ingestion pipeline, account/trade persistence, health/admin endpoints.
- **Frontend (`frontend/`)**: Vercel-hosted landing + `/app` SPA, onboarding gate, Telegram login widget, analytics UI.
- **Extension (`extension/`)**: FundingPips scraper bridge (legacy-compatible connector) that posts live state and closed trades.
- **Data plane**: Neon PostgreSQL behind backend persistence.

## Connector-first ingestion model (2026 pivot)

TaliTrade now uses a connector-first ingestion layer. The FundingPips extension remains fully supported, but it is treated as one connector that feeds canonical ingestion services.

### Canonical ingestion routes

- `POST /ingest/accounts`
- `POST /ingest/account-snapshots`
- `POST /ingest/positions`
- `POST /ingest/trades`
- `POST /ingest/events`
- `POST /ingest/csv/trades` (non-extension connector path)

> Primary app ingestion routes are bearer-authenticated session routes. User ownership is resolved from the authenticated session, not from explicit identity payload fields.

### Canonical authenticated app routes

- `GET /auth/me` (requires bearer session)
- `GET /connectors/overview` (requires bearer session)
- `POST /auth/link-account` (requires bearer session)
- `POST /ingest/accounts`, `POST /ingest/trades`, `POST /ingest/csv/trades` (require bearer session)

Canonical callers now include:
- `frontend/src/App.jsx` (primary app console flows)
- `frontend/app.html` and `frontend/public/app.html` (static app shell)
- `extension/content.js` account-link retry path, now using bearer session from extension storage

### Retired compatibility routes

- `POST /auth/session/bridge` → retired (`410 Gone`)
- `POST /auth/link-account/compat` → retired (`410 Gone`)

These routes are no longer part of the supported auth contract. Canonical callers must use bearer-authenticated session flows (`/auth/telegram`, `/auth/telegram/oidc`, `/auth/me`, `/auth/link-account`).

### Backward compatibility

- Existing `/extension/*` routes still work.
- `/extension/data` now translates extension payloads into canonical `trading_accounts`, `account_snapshots`, and `positions`.
- `/extension/trade` now writes through the same normalized ingestion service used by `/ingest/trades`.

### Phase 2 hardening

- Deterministic `trading_accounts` dedup via computed `account_key` unique index (avoids nullable `user_id` conflict gaps).
- Safe stale-position handling (`is_active`, `last_seen_at`, `closed_at`) with guarded deactivation logic.
- Snapshot insert throttling by short dedupe window when values are unchanged.
- Richer normalized trade persistence (`connector_type`, `open_time`, `fees`, `tags`, `source_metadata`, `import_provenance`).
- Phase 2.5 blocker fix: startup-safe connector table initialization ordering and `position_key`-first position identity (legacy symbol/side uniqueness removed).

## Canonical production assumptions
- Telegram bot username: `TaliTradeBot`
- Canonical Telegram login domain: `talitrade.com`
- Canonical app host policy: apex `talitrade.com` is canonical for Telegram login flows.

## Coding priorities (in order)
1. Broken real user flows
2. Auth/account-link failures
3. Onboarding/loading states
4. Analytics/payout/journal correctness
5. Extension scrape/sync reliability
6. Backend/frontend/extension contract consistency
7. Launch-critical observability
8. Subscription gating / launch control if code-fixable in repo

## PR hygiene
- Prefer clean PRs from latest `main`.
- Do not reuse stale PRs.
- Avoid giant mixed-scope changes.

## Subsystem ownership map
- **Backend**: API correctness, auth validation, persistence, Telegram command/webhook behavior.
- **Extension/scraping**: FundingPips extraction reliability, dedup-safe sync cadence, account linking propagation.
- **Frontend**: onboarding, Telegram login UX, dashboard rendering and state handling.
- **Integration/hardening**: cross-subsystem contracts, launch blockers, observability, production safety guards.

## Working rule for Codex tasks
Read `TALITRADE_CONTEXT.md` first, then implement the smallest safe change that fixes the live issue while preserving canonical production assumptions.
