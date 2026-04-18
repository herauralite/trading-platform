# Known-Good Auth + Platform Baseline Contract

Recorded on **April 18, 2026**.

This is the regression contract that must hold before/after future merges.

## What must keep working

1. `/app` loads correctly and resolves to the authenticated app shell.
2. `/app/accounts` loads correctly on direct link and in-app navigation.
3. `/app/connections` loads correctly on direct link and in-app navigation.
4. `/auth/telegram/config` bootstrap works for:
   - React app auth gate (`frontend/src/App.jsx`)
   - homepage demo/global auth modal (`frontend/index.html`)
   - static app gate fallback (`frontend/app.html`, `frontend/public/app.html`)
5. Telegram widget login completes and clears the app gate.
6. Session persistence survives refresh/navigation via stored token + user and `/auth/me` recovery.
7. Homepage demo Telegram sign-in works and demo score save updates leaderboard.
8. Leaderboard save/load flows remain functional after auth.

## High-risk files/surfaces (do not casually change)

- Frontend auth runtime + routing:
  - `frontend/src/App.jsx`
  - `frontend/src/sessionAuth.js`
  - `frontend/src/apiBase.js`
  - `frontend/vercel.json`
- Static auth gates / modal auth flows:
  - `frontend/index.html`
  - `frontend/app.html`
  - `frontend/public/app.html`
  - `frontend/public/tali-api-base.js`
- Backend auth/session config contracts:
  - `backend/app/main.py` (`/auth/telegram/config`, `/auth/telegram`, `/auth/telegram/oidc`, `/auth/me`)

## Must-not-change casually

- Telegram auth payload contract (`access_token` + user identity fields).
- User id normalization compatibility (`telegram_user_id` and `telegramUserId`).
- Session storage keys and token-first hydration behavior.
- `/auth/me` usage for post-login recovery when widget/response user shape is partial.
- Vercel `/app` rewrites that protect deep-link loads.
- Homepage demo flow path that requires Telegram auth prior to score save.

## Manual smoke test checklist after future merges

1. Open `https://www.talitrade.com/app` in a fresh session.
   - Verify Telegram sign-in widget/setup renders.
2. Sign in with Telegram on `/app`.
   - Verify gate clears and app UI loads.
3. Refresh `/app`.
   - Verify session persists and app rehydrates from token + `/auth/me`.
4. Direct-load each route in a new tab:
   - `https://www.talitrade.com/app`
   - `https://www.talitrade.com/app/accounts`
   - `https://www.talitrade.com/app/connections`
5. Open homepage demo modal (`https://www.talitrade.com`).
   - Verify Telegram setup/config loads.
6. Complete demo flow and save score with Telegram auth.
   - Verify success state and leaderboard update.
7. Confirm diagnostics/logs do not show stale backend host usage.

## Automation guardrails in this repo

- Frontend static/unit auth regression checks:
  - `frontend/src/telegramAuthRegression.test.js`
  - `frontend/src/staticTelegramGateRegression.test.js`
  - `frontend/src/taliApiBaseResolverRegression.test.js`
  - `frontend/src/authPlatformBaselineRegression.test.js`
- Backend auth contract checks:
  - `backend/tests/test_auth_platform_baseline.py`

If any of the above fail, treat as a baseline regression and block feature merge until fixed.
