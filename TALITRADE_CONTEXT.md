# TaliTrade — Full Project Context & Handoff
> Paste this file into the next chat as the first message. All code files are attached separately.

---

## WHAT THIS PROJECT IS

**TaliTrade** — a premium SaaS trading platform for prop firm traders (targeting FundingPips).

**Core vision:** "Your entire trading desk in one place." Not a bot. A full platform.

**Architecture (current, connector-first):**
```
Connectors (FundingPips extension, CSV import, future MT5/manual)
    ↓ POST normalized payloads to /ingest/*
Normalization + ingestion services (FastAPI / Railway)
    ↓ stores canonical records in Neon PostgreSQL
Legacy /extension/* compatibility routes
    ↓ translate FundingPips extension payloads into canonical ingest records
Core APIs
    ↓ sends Telegram alerts
Dashboard (app.html / Vercel → talitrade.com/app)
    ↓ reads live data from backend
    ↓ shows analytics, risk, payout eligibility
Telegram Bot
    ↓ /status /today /journal /news /payout /summary /week /help
```

**Why extension instead of direct API:**
FundingPips (Match Trader platform) does not expose a public API. The Chrome extension scrapes the UI to extract trade data and account state.

## PHASE 2 HARDENING STATUS (connector ingestion)

- Added deterministic account dedup keying for `trading_accounts` so nullable `user_id` does not create duplicate logical accounts.
- Added position state lifecycle fields (`is_active`, `last_seen_at`, `closed_at`, `position_key`) and guarded stale-position deactivation.
- Added account snapshot dedupe window to reduce high-frequency write amplification.
- Extended trade ingestion to preserve richer normalized metadata fields without breaking legacy trade readers.

---

## DEPLOYED INFRASTRUCTURE

| Service | URL / Details |
|---------|--------------|
| **Backend** | Railway → `https://trading-platform-production-70e0.up.railway.app` |
| **Frontend** | Vercel → `talitrade.com/app` (GitHub repo connected, auto-deploys on push) |
| **Database** | Neon PostgreSQL (connected via `DATABASE_URL` env var on Railway) |
| **Bot** | Telegram bot, webhook set to `{RAILWAY_URL}/telegram/webhook` on startup |
| **Extension ID** | `aigdpcmfcnnjkbdikpnblooomhjlkcao` |
| **Extension target** | `https://mtr-platform.fundingpips.com` |

---

## ALL FILE LOCATIONS (AUTHORITATIVE)

| File | Purpose | Deploy to |
|------|---------|-----------|
| `main.py` | FastAPI backend — all endpoints, Telegram bot, schedulers | Railway (push to GitHub) |
| `app.html` | Dashboard SPA — gate, analytics, payout, leaderboard | Vercel (push to GitHub, serves at `/app`) |
| `index.html` | Landing page | Vercel (serves at `/`) |
| `content.js` | Chrome extension content script — scrapes FundingPips | Load in Chrome extension folder |
| `background.js` | Chrome extension service worker — bridges talitrade.com → chrome.storage | Load in Chrome extension folder |
| `manifest.json` | Chrome extension manifest | Load in Chrome extension folder |
| `vercel.json` | Vercel routing — `/app` → `app.html` | Vercel (push to GitHub) |

---

## KEY CONSTANTS (DO NOT CHANGE WITHOUT UPDATING BOTH SIDES)

```
RAILWAY_URL     = "https://trading-platform-production-70e0.up.railway.app"
EXTENSION_ID    = "aigdpcmfcnnjkbdikpnblooomhjlkcao"
DEV_PASSWORD    = "tali2024dev"   (gate step 1 password in app.html)

Storage keys:
  chrome.storage.local: 'tali_telegram_uid'    (extension ↔ background.js)
  localStorage:          'tali_user_v1'         (app.html — stores user object)
  sessionStorage:        'tali_pw_ok'           (app.html — password gate flag)
```

---

## ⚠️ MUST-DO BEFORE LAUNCH (PENDING)

1. **Bot username not confirmed** — `TALI_BOT_USERNAME = 'TaliTradeBot'` in `app.html` line ~512. This must match the actual bot @username from BotFather exactly (no @). If wrong, Telegram Login Widget silently fails.

2. **BotFather `/setdomain`** — go to BotFather → your bot → `/setdomain` → enter `talitrade.com`. Required for Telegram Login Widget to work on talitrade.com.

3. **Extension not on Chrome Web Store** — currently loads unpacked (developer mode). For public launch, needs to be submitted to Chrome Web Store.

4. **Trade execution is disabled** — the Buy/Sell order panel in the Chart tab shows "coming soon" and is disabled. This was intentional — execution via the FundingPips API would require reverse-engineering their authenticated API calls. Potential future feature.

---

## HOW THE AUTH FLOW WORKS (END TO END)

```
1. User opens talitrade.com/app
2. Gate step 1: enters password "tali2024dev" → sessionStorage flag set
3. Gate step 2: Telegram Login Widget fires → calls /auth/telegram
4. Backend verifies HMAC hash, upserts user to `users` table
5. Returns user + their linked prop_accounts
6. app.html: populates ACCOUNTS map, sets activeAcct = first account
7. app.html: sends TALI_SET_UID message to extension via chrome.runtime.sendMessage
8. background.js: receives → writes telegramUserId to chrome.storage.local
9. content.js (on FundingPips): reads chrome.storage.local each poll cycle
10. content.js: includes telegramUserId in every /extension/data POST
11. Backend /extension/data: auto-calls db_link_account when telegramUserId present
12. New users get their account linked to Telegram automatically after first poll
```

---

## DATABASE SCHEMA (SIMPLIFIED)

```sql
-- Users (one row per Telegram account)
users (
  telegram_user_id TEXT PRIMARY KEY,
  telegram_username TEXT,
  first_name TEXT, last_name TEXT, photo_url TEXT,
  created_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ
)

-- Prop accounts linked to a user
prop_accounts (
  id SERIAL PRIMARY KEY,
  telegram_user_id TEXT NOT NULL,
  account_id TEXT NOT NULL,          -- e.g. "1917136"
  broker TEXT DEFAULT 'fundingpips',
  account_type TEXT,                  -- e.g. "2step_master"
  account_size INTEGER,               -- e.g. 10000
  label TEXT,
  is_active BOOLEAN DEFAULT TRUE,
  UNIQUE(telegram_user_id, account_id)
)

-- All closed trades (scraper writes, analytics reads)
trades (
  id SERIAL PRIMARY KEY,
  account_id TEXT,
  account_type TEXT, account_size INTEGER,
  symbol TEXT, direction TEXT, volume FLOAT,
  open_price FLOAT, close_price FLOAT,
  pnl FLOAT,
  balance_after FLOAT, equity_after FLOAT,
  daily_loss_used FLOAT, daily_loss_limit FLOAT,
  overall_loss_used FLOAT, overall_loss_limit FLOAT,
  closed_at TIMESTAMPTZ,
  logged_at TIMESTAMPTZ DEFAULT NOW(),
  source TEXT,                         -- 'scraper' | 'realtime'
  telegram_user_id TEXT,
  UNIQUE (account_id, symbol, direction, closed_at, pnl)  -- dedup constraint
)
```

---

## BACKEND ENDPOINTS (ALL)

```
GET  /health                     — liveness check
GET  /health/db                  — DB connectivity check
POST /auth/telegram              — Telegram Login Widget verification + user upsert
GET  /auth/me?telegram_user_id=  — get user + accounts
POST /auth/link-account          — link prop account to Telegram user

POST /telegram/webhook           — Telegram bot commands
GET  /extension/status           — live account state (TTL: 120s)
POST /extension/data             — heartbeat from extension (every 5s)
POST /extension/trade            — write closed trade to DB
GET  /extension/journal          — fetch trades from DB
GET  /extension/journal/stats    — trade count + oldest date
GET  /extension/news             — upcoming high-impact news
GET  /extension/payout           — payout eligibility check
GET  /leaderboard                — real leaderboard from DB

GET  /admin/dedup-trades/preview — preview realtime duplicate rows
DEL  /admin/dedup-trades         — delete realtime duplicate rows
DEL  /admin/purge-corrupt-trades — delete rows where |pnl| > account_size
GET  /test/db                    — count trades in DB
```

---

## TELEGRAM BOT COMMANDS

```
/start   — register + welcome (shows linked accounts if any)
/status  — live risk snapshot (balance, equity, daily/overall loss bars)
/today   — today's trades + P&L
/journal — last 10 closed trades
/news    — upcoming high-impact news next 8 hours
/payout  — payout eligibility check (all criteria with progress bars)
/summary — today's market-close recap (detailed)
/week    — weekly performance report (P&L, WR, PF, best/worst symbol)
/help    — command list
```

All commands are **per-user routed** — each user sees their own account's data.

---

## EXTENSION DATA FLOW (content.js)

```
poll() every 5s:
  1. detectAccountConfig() — reads account ID, type, size from page text
  2. extractData() — scrapes balance, equity, profit, open positions
  3. detectTradeEvents() — detects open/close state changes
  4. checkRules() — calculates daily/overall loss %, fires alerts at 50/80/90%
  5. POST /extension/data — sends live state + queued close notifications

scrapeAndSyncHistory() every 60s:
  1. Clicks Closed Positions tab
  2. First run: sets filter to "Last 365 days"
  3. Scroll loop — scrolls virtualized list until 3 consecutive no-new-rows
  4. For each unseen row: POST /extension/trade (dedup'd by scrapedTradeKeys Set)
```

**Source field:**
- `source='scraper'` — from scrapeAndSyncHistory (authoritative, used for analytics)
- `source='realtime'` — from live close detection (Telegram only, NOT in analytics)

---

## ACCOUNT TYPES & RULES

```python
PHASE_RULES = {
  "2_step_phase1":    { profit_target_pct: 8.0,  daily_loss_pct: 5.0, max_loss_pct: 10.0 },
  "2_step_phase2":    { profit_target_pct: 5.0,  daily_loss_pct: 5.0, max_loss_pct: 10.0 },
  "2_step_pro_phase1":{ profit_target_pct: 6.0,  daily_loss_pct: 3.0, max_loss_pct: 6.0  },
  "2_step_pro_phase2":{ profit_target_pct: 6.0,  daily_loss_pct: 3.0, max_loss_pct: 6.0  },
  "1_step_phase1":    { profit_target_pct: 10.0, daily_loss_pct: 3.0, max_loss_pct: 6.0  },
  "zero":             { trailing_loss: True,      daily_loss_pct: 3.0, max_loss_pct: 5.0  },
  "2_step_master":    { is_master: True,          daily_loss_pct: 5.0, max_loss_pct: 10.0, payout_split: 0.80 },
  "2_step_pro_master":{ is_master: True,          daily_loss_pct: 3.0, max_loss_pct: 6.0,  payout_split: 0.80 },
}
```

---

## KNOWN ISSUES / ACTIVE BUGS

1. **Scroll loop may not be finding correct container** — if FundingPips has >6 closed trades but only 6 are being scraped, the virtualized list scroll container selector may be wrong. Add `console.log` of which container was found to debug.

2. **TALI_BOT_USERNAME placeholder** — see above. Must be set correctly before Telegram login works for any user.

3. **Leaderboard "You" highlight** — highlights based on `accountId === activeAcct`. Works but only for the currently selected account.

4. **New user empty state** — if a user logs in via Telegram but hasn't opened FundingPips with the extension yet, ACCOUNTS map is empty and `activeAcct = ''`. The onboarding card now shows but the payout/analytics pages still show loading states indefinitely.

---

## COMPLETED THIS SESSION (latest)

1. Fixed `send_daily_summary` — now accepts `chat_id` + `tg_uid` so `/summary` routes to each user's own chat (was silently sending to owner only)
2. Fixed daily auto-broadcast — now fans out to ALL registered users at 5:05 PM ET
3. Added `/week` Telegram command — weekly P&L, WR, PF, best/worst symbol
4. Added real `/leaderboard` endpoint — queries DB for actual top traders
5. Analytics page — added Avg Win, Avg Loss, Best Symbol, Green Streak stat boxes
6. Equity curve — now colors green/red based on overall trajectory
7. Leaderboard (Challenges tab) — replaced fake hardcoded names with real API data; highlights "You" row
8. New user onboarding card — 2-step guide shown when extension not yet connected
9. Trade execution button — marked "coming soon" and disabled (was live dead UI)
10. `lbProfit` null guard added (was crashing if challenges page loaded before live data)

---

## STYLE / HOW TO WORK WITH THIS USER

- Direct, no fluff
- Paste full working code — not diffs
- Deploy instructions: kindergarten-simple (copy this file → paste that file)
- User pastes console logs to debug
- Prefers single-file delivery
- Works in long sessions, fixes bugs one at a time
- User is the sole developer/operator — building toward public launch

---

## IMMEDIATE NEXT PRIORITIES (launch blockers)

1. Confirm bot username with BotFather and update `TALI_BOT_USERNAME` in `app.html`
2. Run `/setdomain talitrade.com` in BotFather
3. Test Telegram login flow end to end on talitrade.com/app
4. Verify scroll loop actually scrolling (check console logs on FundingPips — should see "Scraped X closed positions" where X > 6 if account has more)
5. Submit extension to Chrome Web Store (for public users — they can't load unpacked)
6. Set up a real `TELEGRAM_CHAT_ID` env var on Railway for broadcast fallback
7. Add subscription gating (Stripe or manual) before opening to public

---

## ENV VARS (Railway)

```
DATABASE_URL          — Neon PostgreSQL connection string
TELEGRAM_BOT_TOKEN    — from BotFather
TELEGRAM_CHAT_ID      — owner's personal chat ID (fallback for broadcast)
PRIMARY_ACCOUNT_ID    — "1917136" (fallback account for unlinked users)
WEB_CONCURRENCY       — must be "1"
LOG_LEVEL             — "INFO"
```
