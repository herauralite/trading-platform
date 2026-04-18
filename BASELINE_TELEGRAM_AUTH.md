# Telegram Auth Baseline

Status recorded after production verification.

## Baseline that must not regress

As of this checkpoint, both Telegram auth flows are confirmed working in production:

- `https://www.talitrade.com/app` Telegram login flow works end-to-end.
- The `/app` flow no longer gets stuck on the **Link Telegram** gate after successful login.
- `https://www.talitrade.com` homepage/demo Telegram sign-in flow works.
- Demo leaderboard save/login flow works.
- Backend config endpoint is live at:
  - `https://api.talitrade.com/auth/telegram/config`

## Required frontend API-base behavior

Frontend auth/config requests must resolve API base in this order:

1. `window.__TALI_CONFIG__.apiBase` or `window.__TALI_CONFIG__.api_base`
2. build-time injected backend URL when present
3. explicit production fallback: `https://api.talitrade.com`

Do **not** allow production auth surfaces to fall back to blank API base or relative `/auth/*` requests on the static Vercel host unless a real same-origin backend proxy is intentionally configured.

## Files that must stay aligned

Any future auth/config changes must keep these surfaces using the same backend-resolution strategy:

- `frontend/index.html`
- `frontend/app.html`
- `frontend/public/app.html`
- `frontend/src/apiBase.js`
- `frontend/public/tali-api-base.js`

## Regression checks before merging future auth changes

Verify all of the following before merge:

1. `https://www.talitrade.com/app` loads Telegram config successfully.
2. `https://www.talitrade.com` homepage/demo modal loads Telegram config successfully.
3. Successful Telegram login on `/app` reaches the app and does not loop back to gate.
4. Demo login still saves/loads leaderboard correctly.
5. No stale backend references remain to:
   - the previously retired Railway host

## Notes

This file is a guardrail. Treat the current production behavior above as the known-good baseline before making further auth refactors.


## Working baseline note

As of April 18, 2026, this baseline is the production reference. Auth hardening changes should be incremental and must not redesign the Telegram login flow.
