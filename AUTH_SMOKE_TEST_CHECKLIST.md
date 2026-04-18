# Telegram Auth Smoke Test Checklist

Use this checklist before merging any frontend auth/config changes.

## Production baseline backend

- `https://trading-platform-production-0614.up.railway.app`

## Required checks

1. Homepage modal (`/`) loads Telegram config successfully.
   - Verify diagnostics include:
     - `resolved_api_base`
     - `config_url`
     - `config_fetch_status`
     - `config_fetch_content_type`
     - `config_fetch_error_name`
     - `config_fetch_error_message`
2. `/app` gate loads Telegram config successfully.
3. Successful Telegram login clears **Link Telegram** gate and enters app UI.
4. Post-login `/auth/me` succeeds and user normalization includes both:
   - `telegramUserId`
   - `telegram_user_id`
5. Leaderboard save still works after auth (homepage demo flow).
6. Confirm API-base priority behavior is unchanged:
   1. `window.__TALI_CONFIG__.apiBase` / `api_base`
   2. build-time backend URL
   3. `https://trading-platform-production-0614.up.railway.app`
7. Confirm no stale backend host references remain:
   - the previously retired Railway host

## Guardrail

Do not ship auth changes that cause static pages to silently fall back to relative `/auth/*` calls unless a same-origin backend proxy is explicitly configured.
