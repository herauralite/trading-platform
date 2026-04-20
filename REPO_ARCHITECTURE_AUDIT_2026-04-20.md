# TaliTrade Repository Architecture Audit (2026-04-20)

## 1. REPO STATE SUMMARY
TaliTrade is currently a FastAPI monolith with a connector-first ingestion core and strong backward compatibility for the legacy FundingPips extension and static app shell. Canonical authenticated session flows (`/auth/me`, `/ingest/*`, `/connectors/*`) are live, while legacy `/extension/*` and `prop_accounts` bridges are still actively used to keep production behavior stable.

## 2. WHAT EXISTS NOW
- Canonical connector ingestion services exist and are wired: `upsert_trading_account`, `ingest_account_snapshot`, `ingest_position`, `ingest_trade`, sync queue, and lifecycle state management.
- Startup guards and operational safety are in place, including `WEB_CONCURRENCY` single-process enforcement and startup table initialization via lifespan.
- Telegram auth/session stack exists with both legacy widget verification and OIDC path support, plus bearer token issuance and `/auth/me` session validation.
- Legacy extension ingestion still works and now writes through canonical services from `/extension/data` and `/extension/trade`.
- Telegram webhook command surface is implemented (`/start`, `/status`, `/today`, `/journal`, `/news`, `/payout`, `/summary`, `/week`, `/help`) and routes by Telegram user identity.
- Connector management APIs are present (`/connectors/catalog`, `/connectors/overview`, `/connectors/{connector}/config`, `/connectors/{connector}/sync`, connect/disconnect), plus account workspace routes.
- Provider onboarding scaffolding exists for TradingView, Alpaca, TradeLocker, MT5 bridge pairing/registration, and beta public API connectors.
- Frontend React app (`frontend/src/App.jsx`) consumes canonical auth and connector APIs, supports add-account flows, and includes TradeLocker onboarding inputs and connect calls.
- Static app shell (`frontend/app.html` and `frontend/public/app.html`) keeps canonical `/auth/telegram/config` + `/auth/me` + extension messaging (`TALI_SET_SESSION`) contracts.
- Extension background/content scripts handle UID+session propagation and account linking via canonical `/auth/link-account` bearer flow.

## 3. WHAT IS PARTIAL OR FRAGILE
- Mixed architecture remains in production path: both canonical `trading_accounts` and legacy `prop_accounts` are read/merged, creating complexity and multiple truth sources.
- `account_data_store` is in-memory process state for live status/Telegram bot commands; restart loses live context and webhook commands can return "no live data" until extension polls again.
- `/extension/trade` `TradeData` model lacks `telegramUserId` but code attempts to read it, so user ownership is implicit via account backfill instead of explicit in route payload handling.
- Polling/sync safety is process-bound; while sync run leasing exists, architecture intentionally depends on single process and would be unsafe if `WEB_CONCURRENCY` changed.
- `routers/accounts.py` exposes an old JWT/UUID/`linked_accounts` flow that does not match the canonical Telegram session model and can confuse future contributors.
- ORM models in `backend/app/models/user.py` and `linked_account.py` represent a different schema contract than the runtime SQL bootstrap (`users.telegram_user_id`, `prop_accounts`) and are effectively disconnected from the main path.
- TradeLocker support is real but still beta-like: credential validation and sync plumbing are present, yet broader analytics and user-facing maturity signals are still tied to FundingPips-first assumptions.

## 4. STALE / LEGACY / COMPATIBILITY LAYERS
- Explicitly retired compatibility endpoints are preserved as `410 Gone`: `/auth/session/bridge` and `/auth/link-account/compat`.
- Legacy extension endpoints `/extension/*` are still active and should be treated as compatibility ingress translators, not expanded as first-class new behavior.
- `prop_accounts` table and `db_link_account` bridge/backfill logic are compatibility layers used to avoid breaking existing extension-linked users.
- `/journal/trade` is a backward-compat alias to `/extension/trade`.
- Old account router + ORM (`backend/app/routers/accounts.py`, `backend/app/models/*.py`) appears legacy/side-path and should not be expanded without full contract reconciliation.

## 5. MOST IMPORTANT NEXT STEP
**Implement a canonical “FundingPips connector hydration” pass that upgrades legacy-linked users into fully canonical connector rows and observability, without removing compatibility paths.**

Why this is highest leverage now:
- It reduces ambiguity between `prop_accounts` and `trading_accounts` without breaking current users.
- It hardens connector overview/workspace/session behavior for launch by making FundingPips state consistently canonical.
- It preserves current extension and Telegram flows while reducing ongoing complexity and risk in every future connector feature.

## 6. IMPLEMENTATION PLAN
1. **Backend: add explicit hydration service**
   - File: `backend/app/services/account_workspace.py` (or new `backend/app/services/fundingpips_hydration.py`)
   - Add a function that scans active `prop_accounts` for a user and ensures matching `trading_accounts` + lifecycle rows are present and normalized.

2. **Backend: invoke hydration at auth/session entry points**
   - File: `backend/app/main.py`
   - Call hydration in `/auth/me` and successful `/auth/telegram` / `/auth/telegram/oidc` responses before account reads.

3. **Backend: tighten compatibility semantics**
   - File: `backend/app/main.py`
   - Keep `prop_accounts` writes, but add explicit metadata marker (`compat_source`) and a log counter for users still depending on legacy-only rows.

4. **Backend: add regression tests for hydrated account visibility**
   - File: `backend/tests/test_auth_platform_baseline.py` or new `backend/tests/test_fundingpips_hydration.py`
   - Validate that a user with only `prop_accounts` still gets canonical connector overview/workspaces post-login.

5. **No frontend contract break**
   - Keep `/extension/*`, static app messaging, and bearer session format unchanged.

## 7. OUTPUT FORMAT FOR CODE WORK
For future implementation tasks, provide full updated file contents (copy/paste ready), not diffs.
