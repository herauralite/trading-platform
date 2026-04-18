# /app Account-Centric Refactor Plan (Incremental, Non-Breaking)

Date: 2026-04-18  
Scope: `frontend/` + `backend/app/` architecture analysis and phased migration plan.  
Hard guardrails honored: `BASELINE_TELEGRAM_AUTH.md`, `AUTH_SMOKE_TEST_CHECKLIST.md`.

---

## Guardrails (must remain unchanged)

1. Do **not** redesign Telegram auth or session token behavior (`/auth/telegram`, `/auth/telegram/oidc`, `/auth/me`).
2. Preserve frontend API-base precedence exactly as documented in baseline/auth checklist.
3. Keep auth/CORS/security posture as-is while refactoring IA/domain boundaries.
4. Keep compatibility paths that bridge legacy extension flows while introducing account-centric surfaces.

---

## A) Current-state audit

## 1) Current /app information architecture + navigation reality

### Frontend surfaces are split

- `/app` is currently rewritten to static `frontend/app.html` via Vercel rewrite, which means production `/app` is still strongly tied to legacy extension-era page architecture.
- There is also a React app at `frontend/src/App.jsx`, but this is a separate app surface and currently organizes UX around **Session + Connector Management + Manual/CSV panels**, not a true account workspace router.

**Risk:** two parallel app surfaces produce duplicated domain logic and inconsistent IA evolution.

### React `/src/App.jsx` IA is connector-first, panel-based

Current sections in one page:
- Session/auth gate
- Connector Management
- Manual Journal actions
- CSV Import actions
- All Accounts list

No explicit route model exists for:
- global account switcher
- all-accounts dashboard workspace
- per-account workspace route
- primary account preference UX

### Static `app.html` still carries extension-era product center

Legacy monolith still includes extension-era flows/state and visual framing (single-page script-heavy architecture), and historically assumes extension heartbeat semantics.

**Implication:** even where account switcher UI exists in static page, the domain model is legacy-centric and coupled to extension assumptions.

---

## 2) Backend assumptions still single-broker / single-account / extension-biased

## Strong progress already made (good base)

- Canonical `trading_accounts` model exists with `connector_type`, `external_account_id`, `broker_name`, etc.
- Ingestion supports normalized connector payloads (`/ingest/*`) with session-bound user scoping.
- Connector lifecycle/config/sync-run tables and APIs exist.
- Legacy `prop_accounts` is bridged into canonical model with backfill + fallback logic.

## Remaining implicit legacy assumptions

1. **Legacy FundingPips naming + defaulting still embedded**
   - `CONNECTOR_CATALOG` hardcodes `fundingpips_extension` as primary live-sync connector.
   - `link-account` default broker is still `fundingpips`.

2. **Connector overview is the top-level read model**
   - `/connectors/overview` groups by connector first, then nests accounts.
   - This shape drives frontend UX into connector-first IA instead of account-first IA.

3. **Primary-account concept is not canonicalized for app workspace**
   - Legacy `LinkedAccount` ORM has `is_primary`, but canonical `trading_accounts` pathway does not provide a unified `primary` selection contract consumed by `/app`.

4. **Some account routes still tied to legacy account credential model**
   - `backend/app/routers/accounts.py` is MatchTrade credential-oriented (`account_login`, `password`, `server`, `broker_type='matchtrade'`), separate from canonical connector account model.

---

## 3) Frontend flows already reusable for multi-broker future

These can be preserved and wrapped by new IA instead of rewritten:

- Session bootstrap/hydration + auth normalization guardrails (`telegram_user_id` and `telegramUserId` compatibility).
- Connector catalog + overview loading flow.
- Connector config drafts and secret handling (`hasSecret`, write-only behavior).
- Manual account/trade and CSV import actions (can become account-source onboarding tools).
- Sync-run diagnostics formatting and connector operational status UI primitives.

---

## B) Target architecture (safe, incremental)

## 1) Account domain model (frontend and API read model)

Introduce an **account-centric read model** without breaking existing tables:

```ts
AccountWorkspace {
  account_key: string
  trading_account_id: number
  user_id: string
  external_account_id: string
  display_label: string | null
  broker_name: string | null
  broker_family: string          // normalized broker grouping
  connector_type: string
  connection_status: 'connected' | 'degraded' | 'disconnected' | 'sync_error'
  sync_state: 'idle' | 'queued' | 'running' | 'retrying' | 'failed' | 'succeeded'
  account_type: string | null
  account_size: number | null
  last_activity_at: string | null
  last_sync_at: string | null
  is_primary: boolean
}
```

Key rule: account identity for workspace selection should use canonical `account_key` (already deterministic in ingest service).

## 2) Broker adapter boundaries

Keep broker-specific logic behind adapter/service boundaries:

- `ConnectorAdapter` contract per connector type (fundingpips extension now, future broker connectors later).
- Connector-specific config validation remains per adapter; avoid branching logic in page components.
- UI consumes normalized status + capabilities, not connector-specific request details.

## 3) Frontend navigation target

Adopt route structure that centers accounts:

- `/app` → redirects to `/app/accounts` (all accounts overview)
- `/app/accounts` → portfolio/all-accounts dashboard
- `/app/accounts/:accountKey` → per-account workspace
- `/app/connections` → broker connection center
- `/app/settings/accounts` → account preferences (primary account)

Preserve current auth gate and bootstrap behavior before route entry.

## 4) Global account switcher behavior

- Visible across authenticated routes.
- Data source: normalized `accounts` read model (not connector cards).
- Selection precedence:
  1. explicit route `:accountKey`
  2. stored primary account
  3. first active account
- Switching updates route; no hidden mutable global account in component-local state.

## 5) All-accounts vs single-account views

- **All-accounts view** aggregates totals and health across accounts/connectors.
- **Per-account view** shows account-specific trading, risk, journal, sync history.
- Connector information is shown as metadata/pills/status within account views.

## 6) Broker connection center

Dedicated page for:
- connector status matrix
- connect/reconnect/disconnect actions
- config/credential lifecycle
- sync diagnostics

This removes connector controls from being the main dashboard narrative.

## 7) Portfolio-level vs account-level analytics split

- Portfolio-level endpoints aggregate across active accounts.
- Account-level endpoints key off `account_key`.
- Keep legacy extension analytics routes intact during transition; add new normalized API layer first.

---

## C) Phased implementation plan (incremental)

## Phase 1 — IA shell + routing foundation (lowest risk)

Goal: change app structure, not auth/data contracts.

1. Introduce route shell in React app with guarded auth bootstrap.
2. Add top-level layout with:
   - global account switcher placeholder
   - nav tabs: Accounts, Connections
3. Keep existing connector and manual/csv panels mounted under Connections route initially.
4. Keep all current API calls intact.

**Primary files likely involved**
- `frontend/src/main.jsx`
- `frontend/src/App.jsx` (split into shell + pages)
- new: `frontend/src/pages/AccountsOverviewPage.jsx`
- new: `frontend/src/pages/ConnectionsPage.jsx`
- new: `frontend/src/components/AccountSwitcher.jsx`

**Risk controls**
- No auth flow edits.
- No API-base behavior edits.
- No backend schema edits.

## Phase 2 — Unified account read model (backend + frontend service layer)

Goal: account-first data contract while preserving existing routes.

1. Add backend endpoint(s):
   - `GET /accounts/workspaces` (list)
   - `GET /accounts/workspaces/{account_key}` (detail)
2. Build response by composing existing canonical tables + connector lifecycle + sync state.
3. Keep `/connectors/*` unchanged for compatibility.
4. Add frontend `accountService` that maps new endpoint into UI model.

**Primary files likely involved**
- `backend/app/main.py` (new read endpoints)
- potentially new module: `backend/app/services/account_workspace.py`
- `frontend/src/api` (new service wrapper)

**Risk controls**
- Read-only endpoints first.
- No destructive migration.
- Preserve legacy fallback behavior while canonicalization matures.

## Phase 3 — All-accounts overview page

Goal: make accounts the default home view.

1. Implement `/app/accounts` using workspace list endpoint.
2. Show totals, account cards, connection health per account.
3. Move existing “All Accounts” list logic out of connector page.

**Dependencies**
- Phase 2 read model available.

## Phase 4 — Broker connection center

Goal: isolate connector operations in one operational page.

1. Move connector connect/sync/disconnect/config UI into `/app/connections`.
2. Introduce “Reconnect” action semantics (alias to connect+sync where supported).
3. Keep connector-specific config forms behind adapter metadata.

**Dependencies**
- connector catalog/spec remains source of truth.

## Phase 5 — Trading + analytics normalization

Goal: full platform behavior for portfolio + account workspace.

1. Add portfolio analytics endpoints and account-specific analytics keyed by `account_key`.
2. Refactor existing analytics views to consume normalized services.
3. Keep legacy extension endpoints operational until parity is verified.

---

## D) Immediate low-risk changes (recommended next)

These are safe to start immediately and align with direction without regression risk:

1. **Introduce frontend domain slices without changing behavior**
   - Extract `session`, `connectors`, `accounts` state from monolithic `App.jsx` into hooks/services.
   - Keep same API calls and UI output.

2. **Add account-switcher state contract (internal only)**
   - Create `selectedAccountKey` state with fallback rules, initially sourced from derived accounts list.
   - Do not yet gate all widgets by selected account.

3. **Add read-only backend account workspace endpoint**
   - Compose from existing tables, no write path changes.
   - Feature-flag usage in frontend.

4. **Create Connections page shell and move existing connector card component unchanged**
   - Relocation only; business logic untouched.

5. **Add compatibility tests/checklist for auth before each phase merge**
   - Explicitly run smoke checklist to verify Telegram flow remains baseline-compliant.

---

## Exact files/components/routes/state areas to prioritize

## Frontend

- `frontend/src/App.jsx`
  - Split responsibilities: auth bootstrap, data orchestration, page rendering.
- `frontend/src/main.jsx`
  - Add router and authenticated app shell.
- `frontend/src/sessionAuth.js`
  - Keep untouched except strict compatibility tests.
- `frontend/src/apiBase.js`
  - Keep untouched (guardrail).
- `frontend/src/connectorConfig.js`
  - Keep as adapter-driven config helper; extend via metadata, not branching in pages.

## Backend

- `backend/app/main.py`
  - Keep auth and connector endpoints stable.
  - Add account workspace read endpoints via new composition functions.
- `backend/app/services/connector_ingest.py`
  - Reuse canonical identity primitives (`compute_account_key`) for new workspace API.
- `backend/app/routers/accounts.py`
  - Mark legacy/credential-oriented path as non-primary for `/app`; avoid mixing this model into new UI workspace model.

## Legacy compatibility (do not remove yet)

- `frontend/app.html` and `frontend/public/app.html`
  - Treat as compatibility/legacy surface during migration; avoid mixing new account architecture work into these static monolith files until route ownership is finalized.

---

## Why this path avoids regression

1. It keeps Telegram auth code paths and API-base resolution untouched (highest-risk area per baseline docs).
2. It introduces account-centric behavior first as **read-model + IA shell**, not destructive schema rewrites.
3. It preserves connector APIs and legacy fallbacks while new account surfaces are validated.
4. It isolates broker-specific behavior into adapters/config specs, preventing cross-app scattering.
5. It allows shipping value in phases with rollback points at each layer (route shell, read model, view migration, analytics).

