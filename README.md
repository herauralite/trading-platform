# TaliTrade Repository Guide

TaliTrade is a premium SaaS trading platform for prop firm traders. It combines live account ingestion, analytics, Telegram alerting, and a web dashboard so traders can run their desk from one place.

## Source of truth
- `TALITRADE_CONTEXT.md` is the current-state operational source of truth for deployment, auth flow, endpoints, and launch status.
- The original 25-file TaliTrade model is the intended blueprint, not an assumption that every detail is already implemented in this repo.

## Current architecture
- **Backend (`backend/`)**: FastAPI API, Telegram webhook/bot logic, account/trade persistence, health/admin endpoints.
- **Frontend (`frontend/`)**: Vercel-hosted landing + `/app` SPA, onboarding gate, Telegram login widget, analytics UI.
- **Extension (`extension/`)**: FundingPips scraper bridge that posts live state and closed trades to backend.
- **Data plane**: Neon PostgreSQL behind backend persistence.

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
