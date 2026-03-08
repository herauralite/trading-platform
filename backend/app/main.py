import os
import logging
import httpx
import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, date, timezone, timedelta

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

if int(os.getenv("WEB_CONCURRENCY", "1")) > 1:
    raise RuntimeError("Set WEB_CONCURRENCY=1.")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

RAILWAY_URL     = "https://trading-platform-production-70e0.up.railway.app"
PRIMARY_ACCOUNT = os.getenv("PRIMARY_ACCOUNT_ID", "1917136")

# How long live data stays valid — if the extension hasn't polled in this window
# we treat the account as offline rather than serving stale values.
ACCOUNT_DATA_TTL_SECONDS = 120

INDEX_CURRENCIES = {"USD", "CNY"}
INDEX_KEYWORDS   = [
    "non-farm", "nfp", "payroll", "cpi", "inflation", "pce",
    "fomc", "fed", "federal reserve", "powell", "interest rate",
    "gdp", "ism", "pmi", "unemployment", "jobless",
    "retail sales", "consumer confidence", "jolts",
]
WARN_MINUTES        = 10
FRIDAY_CLOSE_HOUR_ET = 17

# ─── Phase / rule tables ──────────────────────────────────────────────────────
# Single source of truth. Aliases (e.g. "master" → "2_step_master") are handled
# by get_phase_rules() so we only store each distinct rule set once.
PHASE_RULES = {
    "2_step_phase1": {
        "label": "2-Step Phase 1 (Student)",
        "is_master": False, "profit_target_pct": 8.0, "min_trading_days": 3,
        "daily_loss_pct": 5.0, "max_loss_pct": 10.0,
        "next_phase": "2_step_phase2", "payout_eligible": False,
    },
    "2_step_phase2": {
        "label": "2-Step Phase 2 (Practitioner)",
        "is_master": False, "profit_target_pct": 5.0, "min_trading_days": 3,
        "daily_loss_pct": 5.0, "max_loss_pct": 10.0,
        "next_phase": "master", "payout_eligible": False,
    },
    "2_step_pro_phase1": {
        "label": "2-Step Pro Phase 1 (Student)",
        "is_master": False, "profit_target_pct": 6.0, "min_trading_days": 1,
        "daily_loss_pct": 3.0, "max_loss_pct": 6.0,
        "next_phase": "2_step_pro_phase2", "payout_eligible": False,
    },
    "2_step_pro_phase2": {
        "label": "2-Step Pro Phase 2 (Practitioner)",
        "is_master": False, "profit_target_pct": 6.0, "min_trading_days": 1,
        "daily_loss_pct": 3.0, "max_loss_pct": 6.0,
        "next_phase": "master", "payout_eligible": False,
    },
    "1_step_phase1": {
        "label": "1-Step Phase 1 (Student)",
        "is_master": False, "profit_target_pct": 10.0, "min_trading_days": 3,
        "daily_loss_pct": 3.0, "max_loss_pct": 6.0,
        "next_phase": "master", "payout_eligible": False,
    },
    "zero": {
        "label": "Zero Challenge (Master)",
        "is_master": True, "profit_target_pct": None, "min_trading_days": 7,
        "daily_loss_pct": 3.0, "max_loss_pct": 5.0,
        "trailing_loss": True, "consistency_score_pct": 15.0, "safety_cushion_pct": 3.0,
        "next_phase": None, "payout_eligible": True,
        "min_payout_pct": 1.0, "payout_split": 0.95,
        "reward_splits": {"bi_weekly": 95},
    },
    # All 2-step and 1-step master variants share the same rules —
    # only one dict stored, get_phase_rules() maps all aliases here.
    "2_step_master": {
        "label": "Master Funded",
        "is_master": True, "profit_target_pct": None, "min_trading_days": 5,
        "daily_loss_pct": 5.0, "max_loss_pct": 10.0,
        "next_phase": None, "payout_eligible": True,
        "min_payout_pct": 2.0, "payout_split": 0.80,
        "reward_splits": {"weekly": 60, "bi_weekly": 80, "monthly": 100, "on_demand": 90},
        "consistency_score_pct": 35.0,
    },
    "2_step_pro_master": {
        "label": "Master Funded (2-Step Pro)",
        "is_master": True, "profit_target_pct": None, "min_trading_days": 5,
        "daily_loss_pct": 3.0, "max_loss_pct": 6.0,
        "next_phase": None, "payout_eligible": True,
        "min_payout_pct": 1.0, "payout_split": 0.80,
        "reward_splits": {"weekly": 80, "daily": 80},
    },
}


def get_phase_rules(account_type: str) -> dict:
    """Resolve account_type string → phase rule dict. Aliases collapse to canonical keys."""
    if not account_type:
        return PHASE_RULES["2_step_phase1"]
    key = account_type.lower().replace(" ", "_").replace("-", "_")
    if key in PHASE_RULES:
        return PHASE_RULES[key]
    # Alias resolution
    if "zero"  in key:                                          return PHASE_RULES["zero"]
    if "pro"   in key and "master" in key:                     return PHASE_RULES["2_step_pro_master"]
    if "pro"   in key and ("phase2" in key or "phase_2" in key): return PHASE_RULES["2_step_pro_phase2"]
    if "pro"   in key:                                         return PHASE_RULES["2_step_pro_phase1"]
    if ("1_step" in key or "one_step" in key or "1step" in key) and "master" in key:
                                                               return PHASE_RULES["2_step_master"]
    if "1_step" in key or "one_step" in key or "1step" in key: return PHASE_RULES["1_step_phase1"]
    if "master" in key:                                        return PHASE_RULES["2_step_master"]
    if "phase2" in key or "phase_2" in key:                    return PHASE_RULES["2_step_phase2"]
    return PHASE_RULES["2_step_phase1"]


# ─── DB helpers ───────────────────────────────────────────────────────────────
# All queries default to source='scraper' so real-time duplicate rows are
# never surfaced in analytics, summaries, or payout checks.
SCRAPER_FILTER = " AND (source = 'scraper' OR source IS NULL)"


async def ensure_trades_table():
    """Create table + run idempotent migrations.
    Each ALTER TABLE runs in its own connection so a duplicate-column or
    duplicate-constraint error doesn't abort the transaction for later steps."""
    from app.core.database import engine

    # Step 1: Create table (safe — IF NOT EXISTS never errors)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trades (
                id                 SERIAL PRIMARY KEY,
                account_id         TEXT,
                account_type       TEXT,
                account_size       INTEGER,
                symbol             TEXT,
                direction          TEXT,
                volume             FLOAT,
                open_price         FLOAT,
                close_price        FLOAT,
                pnl                FLOAT,
                balance_after      FLOAT,
                equity_after       FLOAT,
                daily_loss_used    FLOAT,
                daily_loss_limit   FLOAT,
                overall_loss_used  FLOAT,
                overall_loss_limit FLOAT,
                closed_at          TIMESTAMPTZ,
                logged_at          TIMESTAMPTZ DEFAULT NOW(),
                source             TEXT
            )
        """))

    # Step 2: Dedup constraint — own connection so failure doesn't poison later steps
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                ALTER TABLE trades
                ADD CONSTRAINT trades_dedup
                UNIQUE (account_id, symbol, direction, closed_at, pnl)
            """))
        logger.info("trades_dedup constraint added")
    except Exception:
        pass  # already exists — safe to ignore

    # Step 3: Source column — same isolation pattern
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE trades ADD COLUMN source TEXT"))
        logger.info("trades source column added")
    except Exception:
        pass  # already exists

    # Step 4: Backfill source on pre-migration rows
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE trades SET source = 'scraper'  WHERE source IS NULL AND balance_after IS NULL"))
        await conn.execute(text("UPDATE trades SET source = 'realtime' WHERE source IS NULL AND balance_after IS NOT NULL"))
        await conn.execute(text("UPDATE trades SET source = 'scraper'  WHERE source IS NULL"))

    # Step 5: telegram_user_id column on trades
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE trades ADD COLUMN telegram_user_id TEXT"))
        logger.info("trades telegram_user_id column added")
    except Exception:
        pass  # already exists

    logger.info("trades table ready")


async def ensure_users_tables():
    """Create users and prop_accounts tables — separate connections so the FK resolves."""
    from app.core.database import engine

    # Step 1: users table — drop and recreate if it exists without the right columns
    async with engine.begin() as conn:
        # Check if telegram_user_id column exists
        result = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'telegram_user_id'
        """))
        col_exists = result.fetchone()
        if not col_exists:
            # Table exists but with wrong schema (empty from failed migration) — drop it
            await conn.execute(text("DROP TABLE IF EXISTS users CASCADE"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id    TEXT PRIMARY KEY,
                telegram_username   TEXT,
                first_name          TEXT,
                last_name           TEXT,
                photo_url           TEXT,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at        TIMESTAMPTZ DEFAULT NOW()
            )
        """))

    # Step 2: prop_accounts — no FK constraint to avoid asyncpg pool ordering issues
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS prop_accounts (
                id               SERIAL PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                account_id       TEXT NOT NULL,
                broker           TEXT NOT NULL DEFAULT 'fundingpips',
                account_type     TEXT,
                account_size     INTEGER,
                label            TEXT,
                is_active        BOOLEAN DEFAULT TRUE,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(telegram_user_id, account_id)
            )
        """))

    logger.info("users + prop_accounts tables ready")


async def db_upsert_user(tg_data: dict) -> dict:
    """Insert or update a user from Telegram login data. Returns the user row."""
    from app.core.database import engine
    tg_id = str(tg_data.get("id", ""))
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO users (telegram_user_id, telegram_username, first_name, last_name, photo_url, last_seen_at)
            VALUES (:tg_id, :username, :first_name, :last_name, :photo_url, NOW())
            ON CONFLICT (telegram_user_id) DO UPDATE SET
                telegram_username = EXCLUDED.telegram_username,
                first_name        = EXCLUDED.first_name,
                last_name         = EXCLUDED.last_name,
                photo_url         = COALESCE(EXCLUDED.photo_url, users.photo_url),
                last_seen_at      = NOW()
        """), {
            "tg_id":      tg_id,
            "username":   tg_data.get("username"),
            "first_name": tg_data.get("first_name"),
            "last_name":  tg_data.get("last_name"),
            "photo_url":  tg_data.get("photo_url"),
        })
        result = await conn.execute(
            text("SELECT * FROM users WHERE telegram_user_id = :tg_id"),
            {"tg_id": tg_id}
        )
        return dict(result.mappings().first() or {})


async def db_get_user_accounts(telegram_user_id: str) -> list:
    """Return all prop accounts linked to a Telegram user."""
    from app.core.database import engine
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM prop_accounts WHERE telegram_user_id = :uid AND is_active = TRUE ORDER BY created_at"),
            {"uid": telegram_user_id}
        )
        return [dict(r) for r in result.mappings().all()]


async def db_link_account(telegram_user_id: str, account_id: str, account_type: str = None,
                           account_size: int = None, label: str = None, broker: str = "fundingpips"):
    """Link a prop account to a Telegram user. Safe to call multiple times."""
    from app.core.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO prop_accounts (telegram_user_id, account_id, broker, account_type, account_size, label)
            VALUES (:uid, :acct_id, :broker, :acct_type, :acct_size, :label)
            ON CONFLICT (telegram_user_id, account_id) DO UPDATE SET
                account_type = COALESCE(EXCLUDED.account_type, prop_accounts.account_type),
                account_size = COALESCE(EXCLUDED.account_size, prop_accounts.account_size),
                label        = COALESCE(EXCLUDED.label, prop_accounts.label),
                is_active    = TRUE
        """), {
            "uid":       telegram_user_id,
            "acct_id":   account_id,
            "broker":    broker,
            "acct_type": account_type,
            "acct_size": account_size,
            "label":     label,
        })


def verify_telegram_auth(data: dict) -> bool:
    """Verify Telegram Login Widget data using HMAC-SHA256."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    check_hash = data.get("hash", "")
    data_check = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_check.items()))
    secret_key = hashlib.sha256(token.encode()).digest()
    computed   = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    # Also check auth_date is not older than 24h
    try:
        auth_age = datetime.now(timezone.utc).timestamp() - int(data.get("auth_date", 0))
        if auth_age > 86400:
            return False
    except Exception:
        pass
    return hmac.compare_digest(computed, check_hash)


def parse_dt(val):
    """Parse a datetime string or return as-is if already a datetime."""
    if val is None or isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None


async def db_insert_trade(trade_dict: dict):
    """Insert a closed trade. Rejects price-sized pnl values (close price bug guard)."""
    pnl_val      = trade_dict.get("pnl") or 0
    account_size = trade_dict.get("accountSize") or 10000
    if abs(pnl_val) > account_size:
        logger.warning(
            f"db_insert_trade: rejected pnl={pnl_val} for {trade_dict.get('symbol')} "
            f"— exceeds accountSize={account_size}, likely a close price not a P&L"
        )
        return
    # Ensure datetime fields are actual datetime objects, not strings
    trade_dict = {**trade_dict, "closedAt": parse_dt(trade_dict.get("closedAt"))}
    from app.core.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO trades (
                account_id, account_type, account_size,
                symbol, direction, volume, open_price, close_price, pnl,
                balance_after, equity_after,
                daily_loss_used, daily_loss_limit,
                overall_loss_used, overall_loss_limit, closed_at, source
            ) VALUES (
                :accountId, :accountType, :accountSize,
                :symbol, :direction, :volume, :openPrice, :closePrice, :pnl,
                :balanceAfter, :equityAfter,
                :dailyLossUsed, :dailyLossLimit,
                :overallLossUsed, :overallLossLimit, :closedAt, :source
            )
            ON CONFLICT (account_id, symbol, direction, closed_at, pnl) DO NOTHING
        """), {k: trade_dict.get(k) for k in [
            "accountId", "accountType", "accountSize", "symbol", "direction", "volume",
            "openPrice", "closePrice", "pnl", "balanceAfter", "equityAfter",
            "dailyLossUsed", "dailyLossLimit", "overallLossUsed", "overallLossLimit",
            "closedAt", "source",
        ]})


async def db_get_trades(
    account_id: str = None, limit: int = 50, offset: int = 0,
    order: str = "desc", source: str = "scraper"
) -> list:
    from app.core.database import engine
    order_sql     = "ASC" if order.lower() == "asc" else "DESC"
    source_clause = "" if not source or source == "all" \
        else f" AND (source = :src OR (source IS NULL AND :src = 'scraper'))"
    params: dict  = {"l": limit, "o": offset}
    if source and source != "all":
        params["src"] = source
    async with engine.connect() as conn:
        where = f"account_id=:a{source_clause}" if account_id else f"1=1{source_clause}"
        if account_id:
            params["a"] = account_id
        result = await conn.execute(
            text(f"SELECT * FROM trades WHERE {where} "
                 f"ORDER BY COALESCE(closed_at, logged_at) {order_sql} LIMIT :l OFFSET :o"),
            params,
        )
        return [dict(r) for r in result.mappings().all()]


async def db_get_trade_stats(account_id: str = None) -> dict:
    """Total count + oldest date — lightweight, used for payout countdown seeding."""
    from app.core.database import engine
    where  = "account_id=:a" if account_id else "1=1"
    params = {"a": account_id} if account_id else {}
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT COUNT(*) as total, MIN(closed_at) as oldest FROM trades WHERE {where}"),
            params,
        )
        row = result.mappings().one_or_none()
        if not row:
            return {"total": 0, "oldest_trade_date": None}
        oldest = row["oldest"]
        if oldest and hasattr(oldest, "isoformat"):
            oldest = oldest.isoformat()
        return {"total": row["total"] or 0, "oldest_trade_date": oldest}


async def db_get_trades_for_date(target_date: str, account_id: str = None) -> list:
    from app.core.database import engine
    parsed = date.fromisoformat(target_date)
    where  = f"account_id=:a AND logged_at::date=:d{SCRAPER_FILTER}" if account_id \
             else f"logged_at::date=:d{SCRAPER_FILTER}"
    params = {"d": parsed}
    if account_id:
        params["a"] = account_id
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT * FROM trades WHERE {where} ORDER BY COALESCE(closed_at, logged_at) ASC"),
            params,
        )
        return [dict(r) for r in result.mappings().all()]


async def db_get_trades_today(account_id: str = None) -> list:
    return await db_get_trades_for_date(date.today().isoformat(), account_id)


async def db_count_trading_days(account_id: str = None) -> int:
    """Count distinct trading days — scraper rows only so realtime dupes don't inflate count."""
    from app.core.database import engine
    where  = f"account_id=:a{SCRAPER_FILTER}" if account_id else f"1=1{SCRAPER_FILTER}"
    params = {"a": account_id} if account_id else {}
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT COUNT(DISTINCT COALESCE(closed_at, logged_at)::date) FROM trades WHERE {where}"),
            params,
        )
        return result.scalar() or 0


async def db_get_green_streak(account_id: str = None) -> int:
    streak     = 0
    check_date = date.today()
    for _ in range(30):
        rows = await db_get_trades_for_date(check_date.isoformat(), account_id)
        if not rows:
            check_date -= timedelta(days=1)
            continue
        if sum((r.get("pnl") or 0) for r in rows) > 0:
            streak     += 1
            check_date -= timedelta(days=1)
        else:
            break
    return streak


def row_to_trade(row: dict) -> dict:
    def iso(v): return v.isoformat() if v and hasattr(v, "isoformat") else v
    pnl         = row.get("pnl") or 0
    daily_used  = row.get("daily_loss_used")  or 0
    daily_limit = row.get("daily_loss_limit") or 500
    return {
        "accountId":        row.get("account_id"),
        "accountType":      row.get("account_type"),
        "accountSize":      row.get("account_size"),
        "symbol":           row.get("symbol"),
        "direction":        row.get("direction"),
        "volume":           row.get("volume"),
        "openPrice":        row.get("open_price"),
        "closePrice":       row.get("close_price"),
        "pnl":              pnl,
        "balanceAfter":     row.get("balance_after"),
        "equityAfter":      row.get("equity_after"),
        "dailyLossUsed":    daily_used,
        "dailyLossLimit":   daily_limit,
        "overallLossUsed":  row.get("overall_loss_used"),
        "overallLossLimit": row.get("overall_loss_limit"),
        "dailyPct":         round(daily_used / daily_limit * 100) if daily_limit else 0,
        "closedAt":         iso(row.get("closed_at")),
        "loggedAt":         iso(row.get("logged_at")),
        "source":           row.get("source") or "scraper",
    }


# ─── Payout eligibility ───────────────────────────────────────────────────────
async def evaluate_payout_eligibility(acct_id: str, acct: dict) -> dict:
    account_type = acct.get("accountType", "")
    account_size = acct.get("accountSize") or 10000
    balance      = acct.get("balance")     or account_size
    overall      = acct.get("overallLoss") or {}
    daily        = acct.get("dailyLoss")   or {}
    rules        = get_phase_rules(account_type)

    is_master   = rules["is_master"]
    profit_usd  = balance - account_size
    profit_pct  = round(profit_usd / account_size * 100, 2)
    target_pct  = rules.get("profit_target_pct")
    target_usd  = round(account_size * target_pct / 100, 2) if target_pct else None
    profit_prog = round(profit_pct / target_pct * 100, 1)   if target_pct else None

    min_days       = rules.get("min_trading_days", 5)
    trading_days   = await db_count_trading_days(acct_id)
    days_remaining = max(0, min_days - trading_days)

    overall_pct = overall.get("pct") or 0
    daily_pct   = daily.get("pct")   or 0
    breached    = overall_pct >= 100 or daily_pct >= 100

    checks = {}
    if not is_master and target_pct:
        checks["profit_target"] = {
            "label":    f"Profit target ({target_pct}%)",
            "required": target_usd,
            "current":  round(profit_usd, 2),
            "pct":      min(100, profit_prog or 0),
            "passed":   profit_pct >= target_pct,
        }
    checks["min_trading_days"] = {
        "label":    f"Minimum trading days ({min_days})",
        "required": min_days, "current": trading_days,
        "pct":      min(100, round(trading_days / min_days * 100)) if min_days else 100,
        "passed":   trading_days >= min_days,
    }
    checks["no_breach"] = {
        "label":   "No rule breach", "passed": not breached,
        "current": f"Daily {daily_pct:.0f}% | Overall {overall_pct:.0f}%",
    }
    all_passed = all(c["passed"] for c in checks.values())

    payout_info = None
    if is_master:
        min_payout_pct  = rules.get("min_payout_pct", 2.0)
        payout_split    = rules.get("payout_split", 0.80)
        reward_splits   = rules.get("reward_splits", {})
        payout_amount   = round(profit_usd * payout_split, 2) if profit_usd > 0 else 0
        payout_eligible = all_passed and profit_pct >= min_payout_pct and not breached

        consistency_note = None
        if rules.get("trailing_loss"):
            cs_pct   = rules.get("consistency_score_pct", 15.0)
            cushion  = rules.get("safety_cushion_pct", 3.0)
            consistency_note = {
                "required_pct":       cs_pct,
                "safety_cushion_usd": account_size * cushion / 100,
                "note": f"Biggest winning day ≤{cs_pct}% of total profit. "
                        f"First ${account_size*cushion/100:.0f} ({cushion}%) is safety cushion.",
            }

        payout_info = {
            "eligible":         payout_eligible,
            "profit_usd":       round(profit_usd, 2),
            "profit_pct":       profit_pct,
            "payout_amount":    payout_amount,
            "payout_split":     int(payout_split * 100),
            "reward_splits":    reward_splits,
            "min_profit_pct":   min_payout_pct,
            "min_profit_usd":   account_size * min_payout_pct / 100,
            "days_remaining":   days_remaining,
            "trading_days":     trading_days,
            "min_trading_days": min_days,
            "breached":         breached,
            "checks":           checks,
            "is_zero":          bool(rules.get("trailing_loss")),
            "consistency_note": consistency_note,
        }

    return {
        "accountId":       acct_id,
        "accountType":     account_type,
        "label":           rules["label"],
        "is_master":       is_master,
        "balance":         balance,
        "account_size":    account_size,
        "profit_usd":      round(profit_usd, 2),
        "profit_pct":      profit_pct,
        "target_pct":      target_pct,
        "target_usd":      target_usd,
        "profit_progress": profit_prog,
        "trading_days":    trading_days,
        "min_trading_days":min_days,
        "days_remaining":  days_remaining,
        "breached":        breached,
        "all_passed":      all_passed,
        "next_phase":      rules.get("next_phase"),
        "checks":          checks,
        "payout":          payout_info,
    }


def format_payout_status(ev: dict, short: bool = False) -> str:
    is_master = ev.get("is_master")
    label     = ev.get("label", "Unknown")
    checks    = ev.get("checks", {})
    def ck(p): return "✅" if p else "❌"

    if not is_master:
        target_pct   = ev.get("target_pct")
        profit_pct   = ev.get("profit_pct", 0)
        progress     = ev.get("profit_progress") or 0
        trading_days = ev.get("trading_days", 0)
        min_days     = ev.get("min_trading_days", 5)
        next_phase   = (ev.get("next_phase") or "Master").replace("_", " ").title()
        breached     = ev.get("breached")
        if short:
            return (
                f"📋 <b>{label}</b>\n"
                f"  Target: {profit_pct:.2f}% / {target_pct}%  ({progress:.0f}%)\n"
                f"  Days: {trading_days}/{min_days}  |  {'❌ BREACHED' if breached else '✅ Clean'}\n"
                f"  {'🎯 Criteria met! → '+next_phase if ev.get('all_passed') else '⏳ In progress'}"
            )
        lines = [f"📋 <b>Phase: {label}</b>  →  Next: {next_phase}\n"]
        if "profit_target" in checks:
            c = checks["profit_target"]
            lines.append(f"  {ck(c['passed'])} Profit target: ${c['current']:+.2f} / ${c['required']:.2f}  ({c['pct']:.0f}%)")
        c = checks.get("min_trading_days", {})
        lines.append(f"  {ck(c.get('passed'))} Trading days: {c.get('current',0)}/{c.get('required',5)}")
        c = checks.get("no_breach", {})
        lines.append(f"  {ck(c.get('passed'))} No breach: {c.get('current','')}")
        if ev.get("all_passed"):
            lines.append(f"\n  🎯 <b>All criteria met! Ready to advance to {next_phase}.</b>")
        return "\n".join(lines)

    payout       = ev.get("payout") or {}
    eligible     = payout.get("eligible")
    payout_amt   = payout.get("payout_amount", 0)
    profit_pct   = payout.get("profit_pct", 0)
    min_pct      = payout.get("min_profit_pct", 2)
    trading_days = payout.get("trading_days", 0)
    min_days     = payout.get("min_trading_days", 5)
    split        = payout.get("payout_split", 80)
    breached     = payout.get("breached")
    reward_splits= payout.get("reward_splits", {})
    if short:
        return (
            f"💸 <b>Payout: {'ELIGIBLE ✅' if eligible else 'Not yet ⏳'}</b>"
            f"  ${payout_amt:,.2f} ({split}% split)\n"
            f"  Profit: {profit_pct:.2f}% | Days: {trading_days}/{min_days}"
        )
    lines = [f"💸 <b>{label} — Payout Status</b>\n"]
    lines.append(f"  {ck(profit_pct >= min_pct)} Profit ≥{min_pct}%:  {profit_pct:.2f}%  (${payout.get('profit_usd',0):+.2f})")
    lines.append(f"  {ck(trading_days >= min_days)} Min trading days:  {trading_days}/{min_days}")
    lines.append(f"  {ck(not breached)} No rule breach")
    if payout.get("is_zero") and payout.get("consistency_note"):
        cn = payout["consistency_note"]
        lines.append(f"\n  ℹ️ Consistency: biggest day ≤{cn['required_pct']}% of total profit")
        lines.append(f"  ℹ️ Safety cushion: ${cn['safety_cushion_usd']:.0f} cannot be requested")
    if reward_splits:
        splits_str = "  |  ".join([f"{k.replace('_',' ').title()}: {v}%" for k, v in reward_splits.items()])
        lines.append(f"\n  📅 Reward splits: {splits_str}")
    if eligible:
        lines.append(f"\n  ✅ <b>PAYOUT ELIGIBLE</b>")
        lines.append(f"  Available ({split}% split): <b>${payout_amt:,.2f}</b>")
    else:
        blockers = []
        if profit_pct < min_pct:    blockers.append(f"Need {min_pct - profit_pct:.2f}% more profit")
        if trading_days < min_days: blockers.append(f"{min_days - trading_days} more trading day(s)")
        if breached:                blockers.append("Rule breach detected")
        lines.append(f"\n  ⏳ Not yet eligible: {' · '.join(blockers)}")
    return "\n".join(lines)


# ─── App + middleware ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TaliTrade...")
    await ensure_trades_table()
    await ensure_users_tables()
    await setup_telegram_webhook()
    tasks = [
        asyncio.create_task(news_scheduler()),
        asyncio.create_task(weekend_scheduler()),
        asyncio.create_task(daily_summary_scheduler()),
    ]
    yield
    for t in tasks:
        t.cancel()
    logger.info("Shutting down.")


app = FastAPI(title="TaliTrade", version="4.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mtr-platform.fundingpips.com",
        "https://app.fundingpips.com",
        "https://talitrade.com",
        "https://www.talitrade.com",
    ],
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import auth, accounts
app.include_router(auth.router)
app.include_router(accounts.router)
from app.core.database import engine


@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/health/db")
async def health_db():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}



# ─── Telegram Auth ────────────────────────────────────────────────────────────
class TelegramAuthData(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


@app.post("/auth/telegram")
async def telegram_login(data: TelegramAuthData):
    """
    Verify Telegram Login Widget callback data and upsert the user.
    Returns user profile + their linked prop accounts.
    Called from the web app after the Telegram Login Widget fires.
    """
    payload = data.dict()
    if not verify_telegram_auth(payload):
        return JSONResponse(status_code=401, content={"detail": "Invalid Telegram auth data"})

    user = await db_upsert_user({"id": data.id, "username": data.username,
                                  "first_name": data.first_name, "last_name": data.last_name,
                                  "photo_url": data.photo_url})
    accounts = await db_get_user_accounts(str(data.id))
    logger.info(f"Telegram login: {data.username or data.id} ({len(accounts)} accounts)")
    return {
        "ok": True,
        "user": {
            "telegramUserId": str(data.id),
            "username":       data.username,
            "firstName":      data.first_name,
            "lastName":       data.last_name,
            "photoUrl":       data.photo_url,
        },
        "accounts": [
            {
                "accountId":   a["account_id"],
                "broker":      a["broker"],
                "accountType": a["account_type"],
                "accountSize": a["account_size"],
                "label":       a["label"],
            }
            for a in accounts
        ],
    }


@app.get("/auth/me")
async def get_me(telegram_user_id: str):
    """Return user profile and linked accounts by telegram_user_id."""
    from app.core.database import engine
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM users WHERE telegram_user_id = :uid"),
            {"uid": telegram_user_id}
        )
        user = dict(result.mappings().first() or {})
    if not user:
        return JSONResponse(status_code=404, content={"detail": "User not found"})
    accounts = await db_get_user_accounts(telegram_user_id)
    return {"user": user, "accounts": accounts}


@app.post("/auth/link-account")
async def link_account(
    telegram_user_id: str,
    account_id: str,
    account_type: str = None,
    account_size: int = None,
    label: str = None,
    broker: str = "fundingpips",
):
    """Link a prop account to a Telegram user. Called by the extension after detecting account info."""
    await db_link_account(telegram_user_id, account_id, account_type, account_size, label, broker)
    accounts = await db_get_user_accounts(telegram_user_id)
    return {"ok": True, "accounts": accounts}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ─── Telegram ─────────────────────────────────────────────────────────────────
async def send_telegram(message: str, chat_id: str = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid   = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not cid:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"Telegram error: {e}")


async def setup_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": f"{RAILWAY_URL}/telegram/webhook"},
            )
            logger.info(f"Telegram webhook: {res.json()}")
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")


# ─── Schedulers ───────────────────────────────────────────────────────────────
daily_summary_sent: set = set()

async def daily_summary_scheduler():
    logger.info("Daily summary scheduler started")
    while True:
        try:
            now_utc   = datetime.now(timezone.utc)
            et_offset = -4 if 3 <= now_utc.month <= 11 else -5
            now_et    = now_utc + timedelta(hours=et_offset)
            if now_et.weekday() < 5:
                today_key = now_et.strftime("%Y-%m-%d")
                target    = now_et.replace(hour=17, minute=5, second=0, microsecond=0)
                if abs((now_et - target).total_seconds()) <= 60 and today_key not in daily_summary_sent:
                    daily_summary_sent.add(today_key)
                    # Broadcast to every registered user who has linked accounts
                    from app.core.database import engine
                    try:
                        async with engine.connect() as conn:
                            result = await conn.execute(text(
                                "SELECT DISTINCT telegram_user_id FROM prop_accounts WHERE is_active=TRUE"
                            ))
                            user_ids = [r[0] for r in result.fetchall()]
                    except Exception:
                        user_ids = []
                    if user_ids:
                        for uid in user_ids:
                            try:
                                await send_daily_summary(today_key, tg_uid=uid)
                            except Exception as e:
                                logger.error(f"Daily summary for {uid}: {e}")
                    else:
                        await send_daily_summary(today_key)
        except Exception as e:
            logger.error(f"Daily summary scheduler: {e}")
        await asyncio.sleep(60)


async def send_daily_summary(summary_date: str, chat_id: str = None, tg_uid: str = None):
    # Use the requesting user's primary account if tg_uid is provided, else fall back to global primary
    if tg_uid:
        acct_id = await get_user_primary_account(tg_uid)
    else:
        acct_id  = PRIMARY_ACCOUNT if PRIMARY_ACCOUNT in account_data_store \
                   else (list(account_data_store.keys())[0] if account_data_store else PRIMARY_ACCOUNT)
    rows     = await db_get_trades_for_date(summary_date, account_id=acct_id)
    trades   = [row_to_trade(r) for r in rows]
    acct     = account_data_store.get(acct_id, {})
    balance  = acct.get("balance")
    daily    = acct.get("dailyLoss")   or {}
    overall  = acct.get("overallLoss") or {}
    acct_sz  = acct.get("accountSize") or 10000
    acct_typ = (acct.get("accountType") or "").replace("_", " ").title()
    ev       = await evaluate_payout_eligibility(acct_id, acct)

    if trades:
        pnls      = [t.get("pnl") or 0 for t in trades]
        total_pnl = sum(pnls)
        wins      = [p for p in pnls if p > 0]
        losses    = [p for p in pnls if p <= 0]
        win_rate  = round(len(wins) / len(pnls) * 100)
        best      = max(pnls); worst = min(pnls)
        best_t    = next(t for t in trades if (t.get("pnl") or 0) == best)
        worst_t   = next(t for t in trades if (t.get("pnl") or 0) == worst)
        avg_win   = sum(wins)   / len(wins)   if wins   else 0
        avg_loss  = sum(losses) / len(losses) if losses else 0
        pf        = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else "∞"
    else:
        total_pnl = 0; wins = []; losses = []

    streak      = await db_get_green_streak(account_id=acct_id)
    streak_line = ""
    if streak >= 3:         streak_line = f"🔥 <b>{streak}-day green streak!</b>\n"
    elif streak == 2:       streak_line = "🔥 2-day green streak — stay focused.\n"
    elif streak == 1 and total_pnl > 0: streak_line = "✨ First green day of a new streak.\n"

    day_icon = "😴" if not trades else ("🟢" if total_pnl > 0 else ("⚪" if total_pnl == 0 else "🔴"))

    def ri(pct):
        if pct >= 90: return "🚨"
        if pct >= 75: return "🔴"
        if pct >= 50: return "⚠️"
        return "✅"
    def rb(pct):
        f = round((pct or 0) / 10)
        return "█" * f + "░" * (10 - f)

    d_pct = daily.get("pct") or 0;   d_rem = daily.get("remaining") or 0
    o_pct = overall.get("pct") or 0; o_rem = overall.get("remaining") or 0
    bal_line = f"💰 Balance: <b>${balance:,.2f}</b>  ({'+'if (balance-acct_sz)>=0 else ''}{(balance-acct_sz):,.2f} overall)\n" if balance else ""

    if not trades:
        is_fri   = datetime.strptime(summary_date, "%Y-%m-%d").weekday() == 4
        wknd_note = "Rest up. Markets open Monday 6 PM ET 🌙" if is_fri \
                    else f"{ri(d_pct)} Daily resets midnight GMT+1 🔄\n{ri(o_pct)} Overall: {o_pct}%  (${o_rem:,.0f} remaining)"
        await send_telegram(
            f"😴 <b>Market Close — {summary_date}</b>\n{'─'*28}\n\nNo trades today.\n\n"
            f"{bal_line}{'─'*28}\n{wknd_note}\n\n{'─'*28}\n{format_payout_status(ev, short=True)}",
            chat_id=chat_id,
        )
        return

    trade_lines = "\n".join([
        f"  {'✅' if (t.get('pnl') or 0) > 0 else '❌'} {t.get('symbol','?')} {t.get('direction','?')} "
        f"{'+'if(t.get('pnl') or 0)>=0 else ''}{(t.get('pnl') or 0):.2f}"
        for t in trades
    ])
    await send_telegram(
        f"{day_icon} <b>Market Close — {summary_date}</b>\n"
        f"<i>{acct_typ} · ${acct_sz//1000}K · {acct_id}</i>\n{'─'*28}\n\n"
        f"{streak_line}📊 <b>Today</b>\n  Net P&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b>\n"
        f"  Trades: {len(trades)}  (W:{len(wins)} L:{len(losses)})\n  Win Rate: {win_rate}%  |  PF: {pf}\n"
        f"  Best: {best_t.get('symbol','?')} +{best:.2f}  |  Worst: {worst_t.get('symbol','?')} {worst:.2f}\n"
        f"  Avg W: +{avg_win:.2f}  |  Avg L: {avg_loss:.2f}\n\n<b>Trades</b>\n{trade_lines}\n\n"
        f"{'─'*28}\n<b>Risk Tomorrow</b>\n{ri(d_pct)} Daily: {d_pct}%  {rb(d_pct)}  (${d_rem:,.0f} left)\n"
        f"{ri(o_pct)} Overall: {o_pct}%  {rb(o_pct)}  (${o_rem:,.0f} left)\n\n"
        f"{bal_line}{'─'*28}\n{format_payout_status(ev, short=True)}\n\nSee you tomorrow 🎯",
        chat_id=chat_id,
    )
    logger.info(f"Daily summary sent: {summary_date} | P&L: {total_pnl:.2f}")


weekend_alerted: set = set()

async def weekend_scheduler():
    logger.info("Weekend scheduler started")
    while True:
        try:
            now_utc   = datetime.now(timezone.utc)
            et_offset = -4 if 3 <= now_utc.month <= 11 else -5
            now_et    = now_utc + timedelta(hours=et_offset)
            if now_et.weekday() == 4:
                today_key = now_et.strftime("%Y-%m-%d")
                for warn_hour, warn_min, label, icon in [
                    (16, 0, "1 HOUR", "⚠️"), (16, 30, "30 MINUTES", "🔴"), (16, 45, "15 MINUTES", "🚨")
                ]:
                    key = f"{today_key}_{warn_hour}_{warn_min}"
                    if key in weekend_alerted:
                        continue
                    target = now_et.replace(hour=warn_hour, minute=warn_min, second=0, microsecond=0)
                    if abs((now_et - target).total_seconds()) <= 60:
                        weekend_alerted.add(key)
                        open_accts = [aid for aid, a in account_data_store.items() if a.get("hasPositions")]
                        pos_warn   = (
                            f"\n🔴 <b>OPEN POSITIONS!</b>\nAccounts: {', '.join(open_accts)}\n"
                            f"Profits will NOT count.\n"
                        ) if open_accts else ""
                        await send_telegram(
                            f"{icon} <b>MARKET CLOSES IN {label}</b>\n{'─'*28}\n"
                            f"🗓 Friday close: <b>5:00 PM ET</b>\n"
                            f"📊 Affects: DJI30, NAS100, SP500, Forex, Gold\n{pos_warn}{'─'*28}\n"
                            f"⚠️ Holding over weekend <b>not permitted</b>.\n"
                            f"Profits <b>won't count</b> — close before 5 PM ET."
                        )
        except Exception as e:
            logger.error(f"Weekend scheduler: {e}")
        await asyncio.sleep(60)


news_cache: list = []; news_alerted: set = set(); news_last_fetch = None

def is_index_relevant(event: dict) -> bool:
    currency = (event.get("country") or "").upper()
    title    = (event.get("title")   or "").lower()
    impact   = (event.get("impact")  or "").lower()
    if impact not in ["high", "red", "3"]: return False
    if currency in INDEX_CURRENCIES:       return True
    return any(kw in title for kw in INDEX_KEYWORDS)

async def fetch_news_calendar() -> list:
    global news_cache, news_last_fetch
    now = datetime.now(timezone.utc)
    if news_last_fetch and (now - news_last_fetch).seconds < 3600 and news_cache:
        return news_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if res.status_code == 200:
                news_cache     = res.json()
                news_last_fetch = now
                logger.info(f"News calendar: {len(news_cache)} events fetched")
                return news_cache
    except Exception as e:
        logger.error(f"News fetch error: {e}")
    return news_cache

def parse_event_time(event: dict):
    try:
        date_str = event.get("date", ""); time_str = event.get("time", "")
        if not date_str or not time_str or time_str.lower() in ["", "all day", "tentative"]:
            return None
        dt_date  = datetime.strptime(date_str, "%m-%d-%Y").date()
        time_str = time_str.strip().lower()
        dt_time  = (datetime.strptime(time_str, "%I:%M%p") if ":" in time_str
                    else datetime.strptime(time_str, "%I%p")).time()
        naive    = datetime.combine(dt_date, dt_time)
        offset   = -4 if 3 <= dt_date.month <= 11 else -5
        return naive.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    except Exception:
        return None

async def news_scheduler():
    logger.info("News scheduler started")
    while True:
        try:
            events = await fetch_news_calendar()
            now    = datetime.now(timezone.utc)
            for event in events:
                if not is_index_relevant(event): continue
                et = parse_event_time(event)
                if not et: continue
                mins = (et - now).total_seconds() / 60
                key  = f"{event.get('title','')}_{et.isoformat()}"
                if WARN_MINUTES - 1 <= mins <= WARN_MINUTES + 1 and key not in news_alerted:
                    news_alerted.add(key)
                    await send_news_alert(event, et, round(mins))
                key30 = f"30min_{key}"
                tl    = (event.get("title") or "").lower()
                if any(w in tl for w in ["speech", "fomc", "powell", "fed chair", "testimony"]) \
                        and 29 <= mins <= 31 and key30 not in news_alerted:
                    news_alerted.add(key30)
                    await send_news_alert(event, et, round(mins))
        except Exception as e:
            logger.error(f"News scheduler: {e}")
        await asyncio.sleep(60)

async def send_news_alert(event: dict, event_time: datetime, minutes: int):
    title    = event.get("title", "Unknown")
    currency = (event.get("country") or "").upper()
    forecast = event.get("forecast", ""); previous = event.get("previous", "")
    et_offset = -4 if 3 <= event_time.month <= 11 else -5
    et_time   = event_time + timedelta(hours=et_offset)
    tl        = title.lower()
    guidance  = ""
    if any(w in tl for w in ["fomc", "fed", "powell", "interest rate"]):
        guidance = "⚡ <b>Fed event — expect high volatility.</b>\n"
    elif any(w in tl for w in ["non-farm", "nfp", "payroll"]):
        guidance = "⚡ <b>NFP — biggest mover for indices.</b>\n"
    elif any(w in tl for w in ["cpi", "inflation", "pce"]):
        guidance = "⚡ <b>Inflation data — rate expectations impact.</b>\n"
    forecast_line = f"Forecast: <b>{forecast}</b> | Previous: {previous}\n" if forecast else ""
    await send_telegram(
        f"🗞 <b>HIGH-IMPACT NEWS IN {minutes} MIN</b>\n{'─'*28}\n"
        f"📌 <b>{title}</b>\n🌍 Currency: <b>{currency}</b>\n"
        f"🕐 Time: <b>{et_time.strftime('%I:%M %p ET')}</b>\n"
        f"📊 Affects: <b>DJI30, NAS100, SP500</b>\n"
        f"{forecast_line}{'─'*28}\n{guidance}"
        f"⚠️ No trades within <b>5 min before or after</b> this event."
    )


# ─── In-memory live state ─────────────────────────────────────────────────────
# Keyed by accountId. Each entry is the last ExtensionData payload + last_updated timestamp.
account_data_store: dict = {}


def get_live_account(account_id: str) -> dict:
    """Return account data only if it was updated within ACCOUNT_DATA_TTL_SECONDS.
    Returns an empty dict if stale — callers should treat this as 'extension offline'."""
    acct = account_data_store.get(account_id, {})
    if not acct:
        return {}
    last_updated = acct.get("last_updated")
    if not last_updated:
        return acct
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(last_updated)).total_seconds()
        if age > ACCOUNT_DATA_TTL_SECONDS:
            return {}
    except Exception:
        pass
    return acct


def get_primary_account_id() -> str:
    """Return the primary account ID from live store, falling back to env var."""
    if PRIMARY_ACCOUNT in account_data_store:
        return PRIMARY_ACCOUNT
    return list(account_data_store.keys())[0] if account_data_store else PRIMARY_ACCOUNT


async def get_user_primary_account(telegram_user_id: str) -> str:
    """Get the first active prop account for a Telegram user, fall back to global primary."""
    if not telegram_user_id:
        return get_primary_account_id()
    accounts = await db_get_user_accounts(telegram_user_id)
    if accounts:
        # Prefer an account that's currently live in the store
        for a in accounts:
            if get_live_account(a["account_id"]):
                return a["account_id"]
        return accounts[0]["account_id"]
    return get_primary_account_id()


# ─── Telegram bot commands ────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body     = await request.json()
    message  = body.get("message", {})
    cmd      = message.get("text", "").strip().lower()
    chat_id  = str(message.get("chat", {}).get("id", ""))
    tg_from  = message.get("from", {})
    tg_uid   = str(tg_from.get("id", "")) if tg_from else chat_id

    # ── /start — register or welcome back ────────────────────────────────────
    if cmd in ["/start", "/start@talitrade_bot"]:
        user = await db_upsert_user({
            "id":         tg_from.get("id"),
            "username":   tg_from.get("username"),
            "first_name": tg_from.get("first_name"),
            "last_name":  tg_from.get("last_name"),
        })
        accounts = await db_get_user_accounts(tg_uid)
        name     = tg_from.get("first_name") or tg_from.get("username") or "Trader"
        if accounts:
            acct_lines = "\n".join(
                f"  • {a['account_id']} ({a['account_type'] or a['broker']}, ${(a['account_size'] or 0):,})"
                for a in accounts
            )
            await send_telegram(
                f"👋 Welcome back, <b>{name}</b>!\n\n"
                f"Your linked accounts:\n{acct_lines}\n\n"
                f"Use /status to check your live risk.", chat_id=chat_id
            )
        else:
            await send_telegram(
                f"👋 Welcome to <b>TaliTrade</b>, {name}!\n\n"
                f"Your Telegram account is registered. To get started:\n\n"
                f"1️⃣ Open the platform at talitrade.com/app\n"
                f"2️⃣ Log in with Telegram\n"
                f"3️⃣ Install the Chrome extension on FundingPips\n\n"
                f"Your trading data will automatically link to this account.\n\n"
                f"/help — see all commands", chat_id=chat_id
            )
        return {"ok": True}

    dispatch = {
        "/status":  handle_status,
        "/today":   handle_today,
        "/journal": handle_journal,
        "/news":    handle_news,
    }
    for c, fn in dispatch.items():
        if cmd in [c, f"{c}@talitrade_bot"]:
            await fn(chat_id, tg_uid=tg_uid); return {"ok": True}

    if cmd in ["/payout",  "/payout@talitrade_bot"]:    await handle_payout(chat_id, tg_uid=tg_uid)
    elif cmd in ["/summary", "/summary@talitrade_bot"]:  await send_daily_summary(date.today().isoformat(), chat_id=chat_id, tg_uid=tg_uid)
    elif cmd in ["/week",    "/week@talitrade_bot"]:     await send_weekly_summary(chat_id=chat_id, tg_uid=tg_uid)
    elif cmd in ["/help",    "/help@talitrade_bot"]:
        await send_telegram(
            "🤖 <b>TaliTrade Commands</b>\n\n"
            "/status  — Live risk snapshot\n/today   — Today's trades & P&L\n"
            "/journal — Last 10 trades\n/news    — Upcoming high-impact news\n"
            "/payout  — Payout eligibility check\n/summary — Today's market-close recap\n"
            "/week    — Weekly performance report\n"
            "/help    — This message", chat_id=chat_id,
        )
    return {"ok": True}


async def handle_payout(chat_id: str, tg_uid: str = None):
    acct_id = await get_user_primary_account(tg_uid) if tg_uid else get_primary_account_id()
    acct    = get_live_account(acct_id)
    if not acct:
        await send_telegram("📡 No live data — open FundingPips in your browser first.", chat_id=chat_id)
        return
    ev = await evaluate_payout_eligibility(acct_id, acct)
    await send_telegram(
        f"💸 <b>Payout Check — {acct_id}</b>\n{'─'*28}\n\n{format_payout_status(ev, short=False)}",
        chat_id=chat_id,
    )


async def handle_news(chat_id: str):
    events   = await fetch_news_calendar()
    now      = datetime.now(timezone.utc)
    upcoming = []
    for event in events:
        if not is_index_relevant(event): continue
        et = parse_event_time(event)
        if not et: continue
        mins = (et - now).total_seconds() / 60
        if 0 < mins < 480:
            upcoming.append((mins, event, et))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        await send_telegram("📅 No high-impact news in the next 8 hours.", chat_id=chat_id)
        return
    lines = []
    for mins, event, et in upcoming[:8]:
        et_offset = -4 if 3 <= et.month <= 11 else -5
        et_time   = et + timedelta(hours=et_offset)
        when      = f"in {round(mins)}m" if mins < 60 else f"in {round(mins/60,1)}h"
        lines.append(f"🔴 <b>{event.get('title','?')}</b> — {et_time.strftime('%I:%M %p')} ET ({when})")
    await send_telegram(
        f"📅 <b>Upcoming High-Impact News</b>\n<i>DJI30, NAS100, SP500</i>\n{'─'*28}\n\n"
        + "\n".join(lines)
        + f"\n\n{'─'*28}\n⚠️ No trades within 5 min before/after each event.", chat_id=chat_id,
    )


async def handle_status(chat_id: str, tg_uid: str = None):
    acct_id = await get_user_primary_account(tg_uid) if tg_uid else get_primary_account_id()
    acct    = get_live_account(acct_id)
    if not acct:
        await send_telegram("📡 No live data — open FundingPips in your browser first.", chat_id=chat_id)
        return
    balance  = acct.get("balance") or 0
    equity   = acct.get("equity")  or 0
    profit   = acct.get("profit")  or 0
    risk     = acct.get("riskPerTradeIdea") or {}
    daily    = acct.get("dailyLoss")   or {}
    overall  = acct.get("overallLoss") or {}
    acct_typ = acct.get("accountType", "unknown")
    acct_sz  = acct.get("accountSize", 10000)
    last     = (acct.get("last_updated") or "")[:19].replace("T", " ")

    def bar(pct):
        f = round((pct or 0) / 10)
        return "█" * f + "░" * (10 - f)
    def icon(pct):
        if pct is None: return "⚪"
        if pct >= 90:   return "🚨"
        if pct >= 75:   return "🔴"
        if pct >= 50:   return "⚠️"
        return "✅"

    risk_line = ""
    if risk.get("applicable"):
        risk_line = (
            f"{icon(risk.get('pct'))} <b>Trade Idea Risk</b>  {risk.get('pct',0)}%\n"
            f"  {bar(risk.get('pct',0))}  ${risk.get('combined',0):.0f} / ${risk.get('limit',300):.0f}\n"
            f"  Remaining: <b>${risk.get('remaining',300):.0f}</b>\n\n"
        )
    ev = await evaluate_payout_eligibility(acct_id, acct)
    await send_telegram(
        f"📊 <b>TaliTrade — {acct_id}</b>\n<i>{acct_typ} | ${acct_sz/1000:.0f}K</i>\n{'─'*28}\n\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n📈 Equity: <b>${equity:.2f}</b>\n"
        f"📉 P&L: <b>{'+'if profit>=0 else ''}{profit:.2f}</b>\n\n{'─'*28}\n{risk_line}"
        f"{icon(daily.get('pct'))} <b>Daily Loss</b>  {daily.get('pct',0)}%\n"
        f"  {bar(daily.get('pct',0))}  ${daily.get('used',0):.0f} / ${daily.get('limit',500):.0f}\n"
        f"  Remaining: <b>${daily.get('remaining',500):.0f}</b>\n\n"
        f"{icon(overall.get('pct'))} <b>Overall Loss</b>  {overall.get('pct',0)}%\n"
        f"  {bar(overall.get('pct',0))}  ${overall.get('used',0):.0f} / ${overall.get('limit',1000):.0f}\n"
        f"  Remaining: <b>${overall.get('remaining',1000):.0f}</b>\n\n"
        f"{'─'*28}\n{format_payout_status(ev, short=True)}\n\n🕐 {last} UTC",
        chat_id=chat_id,
    )


async def handle_today(chat_id: str, tg_uid: str = None):
    acct_id      = await get_user_primary_account(tg_uid) if tg_uid else get_primary_account_id()
    today        = date.today().isoformat()
    rows         = await db_get_trades_today(account_id=acct_id)
    today_trades = [row_to_trade(r) for r in rows]
    if not today_trades:
        await send_telegram(f"📅 No trades logged today ({today}).", chat_id=chat_id)
        return
    total_pnl = sum(t.get("pnl") or 0 for t in today_trades)
    wins      = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    win_rate  = round(len(wins) / len(today_trades) * 100)
    lines = [
        f"{'✅' if (t.get('pnl') or 0) > 0 else '❌'} {t.get('symbol','?')} {t.get('direction','?')} "
        f"<b>{'+'if(t.get('pnl') or 0)>=0 else ''}{(t.get('pnl') or 0):.2f}</b> @ {(t.get('closedAt') or '')[11:16]}"
        for t in today_trades
    ]
    await send_telegram(
        f"📅 <b>Today — {today}</b>\n{'─'*28}\n\n" + "\n".join(lines)
        + f"\n\n{'─'*28}\nP&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> "
        f"| {len(today_trades)} trades | WR: {win_rate}%",
        chat_id=chat_id,
    )


async def handle_journal(chat_id: str, tg_uid: str = None):
    acct_id = await get_user_primary_account(tg_uid) if tg_uid else get_primary_account_id()
    rows    = await db_get_trades(account_id=acct_id, limit=10)
    recent  = [row_to_trade(r) for r in rows]
    if not recent:
        await send_telegram("📒 No trades in journal yet.", chat_id=chat_id)
        return
    total_pnl = sum(t.get("pnl") or 0 for t in recent)
    wins      = len([t for t in recent if (t.get("pnl") or 0) > 0])
    lines = [
        f"{'✅' if (t.get('pnl') or 0) > 0 else '❌'} <b>{t.get('symbol','?')}</b> {t.get('direction','?')} "
        f"{'+'if(t.get('pnl') or 0)>=0 else ''}{(t.get('pnl') or 0):.2f} | {(t.get('closedAt') or '')[:10]}"
        for t in recent
    ]
    await send_telegram(
        f"📒 <b>Last {len(recent)} Trades</b>\n{'─'*28}\n\n" + "\n".join(lines)
        + f"\n\n{'─'*28}\nP&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> | WR: {round(wins/len(recent)*100)}%",
        chat_id=chat_id,
    )


# ─── Weekly summary ────────────────────────────────────────────────────────────
async def send_weekly_summary(chat_id: str = None, tg_uid: str = None):
    """Send a weekly P&L and performance digest to the requesting user."""
    acct_id = await get_user_primary_account(tg_uid) if tg_uid else get_primary_account_id()
    from app.core.database import engine
    # Last 7 calendar days
    week_start = (date.today() - timedelta(days=6)).isoformat()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"""
                SELECT * FROM trades
                WHERE account_id=:a AND COALESCE(closed_at, logged_at) >= :ws
                AND (source='scraper' OR source IS NULL)
                ORDER BY COALESCE(closed_at, logged_at) ASC
            """),
            {"a": acct_id, "ws": week_start},
        )
        rows = [dict(r) for r in result.mappings().all()]
    trades = [row_to_trade(r) for r in rows]

    if not trades:
        await send_telegram(
            f"📅 <b>Weekly Report</b>\n{'─'*28}\n\nNo trades this week.",
            chat_id=chat_id,
        )
        return

    pnls      = [t.get("pnl") or 0 for t in trades]
    total_pnl = sum(pnls)
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p < 0]
    win_rate  = round(len(wins) / len(pnls) * 100)
    gp, gl    = sum(wins), abs(sum(losses))
    pf        = round(gp / gl, 2) if gl > 0 else "∞"

    # Best and worst symbols
    sym_pnl: dict = {}
    for t in trades:
        s = t.get("symbol") or "?"
        sym_pnl[s] = sym_pnl.get(s, 0) + (t.get("pnl") or 0)
    best_sym  = max(sym_pnl, key=sym_pnl.get)
    worst_sym = min(sym_pnl, key=sym_pnl.get)

    # Distinct trading days this week
    trading_days = len({(t.get("closedAt") or t.get("loggedAt") or "")[:10] for t in trades if (t.get("closedAt") or t.get("loggedAt"))})

    acct     = account_data_store.get(acct_id, {})
    acct_sz  = acct.get("accountSize") or 10000
    acct_typ = (acct.get("accountType") or "").replace("_", " ").title()
    ev       = await evaluate_payout_eligibility(acct_id, acct)
    icon     = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "⚪")

    await send_telegram(
        f"{icon} <b>Weekly Report</b>\n"
        f"<i>{acct_typ} · ${acct_sz//1000}K · {acct_id}</i>\n"
        f"<i>{week_start} → {date.today().isoformat()}</i>\n{'─'*28}\n\n"
        f"📊 <b>Performance</b>\n"
        f"  Net P&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b>\n"
        f"  Trades: {len(trades)}  (W:{len(wins)} L:{len(losses)})\n"
        f"  Win Rate: {win_rate}%  |  PF: {pf}\n"
        f"  Trading Days: {trading_days}\n\n"
        f"🏆 Best Symbol:  {best_sym}  ({'+' if sym_pnl[best_sym]>=0 else ''}{sym_pnl[best_sym]:.2f})\n"
        f"📉 Worst Symbol: {worst_sym}  ({'+' if sym_pnl[worst_sym]>=0 else ''}{sym_pnl[worst_sym]:.2f})\n\n"
        f"{'─'*28}\n{format_payout_status(ev, short=True)}",
        chat_id=chat_id,
    )
    logger.info(f"Weekly summary sent: {acct_id} | P&L: {total_pnl:.2f}")


# ─── Extension endpoints ──────────────────────────────────────────────────────
class ExtensionData(BaseModel):
    profit: float | None = None
    balance: float | None = None
    equity: float | None = None
    accountId: str | None = None
    accountType: str | None = None
    accountSize: int | None = None
    accountLabel: str | None = None
    isMaster: bool = False
    hasPositions: bool = False
    openPositionCount: int = 0
    positions: list = []
    riskPerTradeIdea: dict | None = None
    dailyLoss: dict | None = None
    overallLoss: dict | None = None
    alerts: list = []
    closedTrades: list = []  # real-time detected closes — Telegram only, NOT written to DB
    timestamp: str | None = None
    url: str | None = None
    telegramUserId: str | None = None  # set by extension after user logs in


class TradeData(BaseModel):
    accountId: str | None = None
    accountType: str | None = None
    accountSize: int | None = None
    symbol: str | None = None
    direction: str | None = None
    volume: float | None = None
    openPrice: float | None = None
    closePrice: float | None = None
    pnl: float | None = None
    balanceAfter: float | None = None
    equityAfter: float | None = None
    dailyLossUsed: float | None = None
    dailyLossLimit: float | None = None
    overallLossUsed: float | None = None
    overallLossLimit: float | None = None
    closedAt: str | None = None
    source: str | None = "realtime"


@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    """
    Main extension heartbeat — receives live account state every 5s.
    Responsibilities:
      1. Store latest account data (with TTL timestamp)
      2. Fire Telegram alerts for new rule violations
      3. Fire Telegram trade-close notifications (real-time, within 5s)
      4. Detect profit drops / breakeven crossovers for live position alerts
    """
    account_id  = data.accountId or "unknown"
    prev        = account_data_store.get(account_id, {})
    prev_alerts = {a.get("type"): a.get("level") for a in (prev.get("alerts") or [])}

    account_data_store[account_id] = {
        **data.dict(),
        "last_updated": datetime.utcnow().isoformat(),
    }

    # Auto-link account to telegram user if telegramUserId is provided
    tg_uid = data.telegramUserId
    if tg_uid and account_id != "unknown":
        try:
            await db_link_account(
                telegram_user_id=tg_uid,
                account_id=account_id,
                account_type=data.accountType,
                account_size=data.accountSize,
                label=data.accountLabel,
            )
        except Exception as e:
            logger.warning(f"Auto-link account failed: {e}")

    # Rule violation alerts — only fire when level changes (prevents spam on restart)
    for alert in data.alerts:
        if prev_alerts.get(alert.get("type")) != alert.get("level"):
            await send_telegram(alert.get("message", "") + f"\n\n<i>Account: {account_id}</i>")

    # Real-time trade close alerts — arrive within 5s of detection
    for ct in data.closedTrades:
        pnl         = ct.get("pnl") or 0
        icon        = "✅" if pnl > 0 else "❌"
        daily_pct   = round((ct.get("dailyLossUsed")  or 0) / (ct.get("dailyLossLimit")  or 500)  * 100)
        overall_pct = round((ct.get("overallLossUsed") or 0) / (ct.get("overallLossLimit") or 1000) * 100)
        bal         = ct.get("balanceAfter")
        bal_line    = f"Balance: ${bal:,.2f}\n" if bal else ""
        await send_telegram(
            f"{icon} <b>Trade Closed</b>\nAccount: {account_id}\n"
            f"{ct.get('symbol','?')} {ct.get('direction','?')} | "
            f"<b>{'+'if pnl>=0 else ''}{pnl:.2f}</b>\n"
            f"{bal_line}Daily: {daily_pct}% | Overall: {overall_pct}%"
        )

    # Live position monitoring
    prev_profit = prev.get("profit"); curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        if prev_profit - curr_profit >= 10:
            await send_telegram(
                f"📉 <b>Profit Drop</b>\nAccount: {account_id}\n"
                f"${prev_profit:.2f} → ${curr_profit:.2f}  (-${prev_profit-curr_profit:.2f})"
            )
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(f"✅ <b>Position in Profit!</b>\nAccount: {account_id} | ${curr_profit:.2f}")

    risk = data.riskPerTradeIdea or {}; daily = data.dailyLoss or {}; overall = data.overallLoss or {}
    return {
        "status": "ok", "account": account_id,
        "balance": data.balance, "equity": data.equity,
        "tradeRisk":   {"used": risk.get("combined"),   "remaining": risk.get("remaining"),   "pct": risk.get("pct")},
        "dailyLoss":   {"used": daily.get("used"),       "remaining": daily.get("remaining"),   "pct": daily.get("pct")},
        "overallLoss": {"used": overall.get("used"),     "remaining": overall.get("remaining"), "pct": overall.get("pct")},
        "alerts_fired": len(data.alerts),
    }


@app.post("/extension/trade")
async def log_trade(trade: TradeData):
    """Scraper-only endpoint. Persists closed trades to DB. No Telegram — that comes via /extension/data."""
    await db_insert_trade(trade.dict())
    return {"status": "ok", "persisted": True}


# Backward-compat alias — older extension versions posted here
@app.post("/journal/trade")
async def log_trade_alias(trade: TradeData):
    return await log_trade(trade)


@app.get("/extension/journal")
async def get_journal(
    account_id: str = None, limit: int = 50, offset: int = 0,
    order: str = "desc", source: str = "scraper"
):
    rows = await db_get_trades(account_id=account_id, limit=limit, offset=offset,
                               order=order, source=source)
    return {"trades": [row_to_trade(r) for r in rows], "total": len(rows),
            "offset": offset, "limit": limit}


@app.get("/extension/journal/stats")
async def get_journal_stats(account_id: str = None):
    return await db_get_trade_stats(account_id=account_id)


@app.get("/extension/status")
async def extension_status():
    """Returns live account state. Stale accounts (>2 min since last poll) are excluded."""
    live = {aid: acct for aid, acct in account_data_store.items() if get_live_account(aid)}
    return {"accounts": live, "count": len(live)}


@app.get("/extension/news")
async def get_news():
    events = await fetch_news_calendar()
    now    = datetime.now(timezone.utc)
    upcoming = []
    for event in events:
        if not is_index_relevant(event): continue
        et = parse_event_time(event)
        if not et: continue
        mins = (et - now).total_seconds() / 60
        if -60 < mins < 480:
            et_offset = -4 if 3 <= et.month <= 11 else -5
            et_time   = et + timedelta(hours=et_offset)
            upcoming.append({
                "title":          event.get("title"),
                "currency":       event.get("country"),
                "time_et":        et_time.strftime("%I:%M %p ET"),
                "time_utc":       et.isoformat(),
                "minutes_until":  round(mins),
                "forecast":       event.get("forecast"),
                "previous":       event.get("previous"),
            })
    upcoming.sort(key=lambda x: x["minutes_until"])
    return {"events": upcoming[:10]}


@app.get("/extension/payout")
async def get_payout(account_id: str = None):
    acct_id = account_id or get_primary_account_id()
    acct    = get_live_account(acct_id)
    return await evaluate_payout_eligibility(acct_id, acct)


# ─── Admin ────────────────────────────────────────────────────────────────────
@app.get("/admin/dedup-trades/preview")
async def dedup_trades_preview():
    """Preview realtime rows that would be deleted by DELETE /admin/dedup-trades."""
    from app.core.database import engine
    async with engine.connect() as conn:
        count_res = await conn.execute(
            text("SELECT COUNT(*) FROM trades WHERE source = 'realtime'")
        )
        total = count_res.scalar()
        sample_res = await conn.execute(text("""
            SELECT id, account_id, symbol, direction, pnl, closed_at, source, balance_after
            FROM trades WHERE source = 'realtime'
            ORDER BY closed_at DESC LIMIT 50
        """))
        rows = [dict(r) for r in sample_res.mappings().all()]
    return {"total_realtime_rows": total, "sample": rows}


@app.delete("/admin/dedup-trades")
async def dedup_trades():
    """Remove realtime-tagged duplicate rows. Safe to run multiple times."""
    from app.core.database import engine
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            DELETE FROM trades WHERE source = 'realtime'
            RETURNING id, account_id, symbol, direction, pnl, closed_at
        """))
        deleted = [dict(r) for r in result.mappings().all()]
    logger.info(f"dedup-trades: removed {len(deleted)} realtime rows")
    return {"status": "ok", "deleted_count": len(deleted), "deleted_rows": deleted}


@app.delete("/admin/purge-corrupt-trades")
async def purge_corrupt_trades():
    """Remove rows where |pnl| > account_size — these are close prices stored as P&L."""
    from app.core.database import engine
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            DELETE FROM trades WHERE ABS(pnl) > account_size
            RETURNING id, account_id, symbol, pnl, account_size
        """))
        deleted = [dict(r) for r in result.mappings().all()]
    logger.info(f"purge-corrupt-trades: removed {len(deleted)} rows")
    return {"status": "ok", "deleted_count": len(deleted), "deleted_rows": deleted}


@app.get("/test/db")
async def test_db():
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM trades"))).scalar()
    return {"status": "ok", "trades_in_db": count}


# ─── Leaderboard ──────────────────────────────────────────────────────────────
@app.get("/leaderboard")
async def get_leaderboard(limit: int = 10):
    """
    Return top traders ranked by total realised P&L (scraper source).
    Names are taken from the users table; anonymised to first-name only.
    """
    from app.core.database import engine
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT
                pa.telegram_user_id,
                COALESCE(u.first_name, 'Trader') AS display_name,
                pa.account_id,
                pa.account_type,
                pa.account_size,
                COUNT(t.id)                            AS trade_count,
                COALESCE(SUM(t.pnl), 0)               AS total_pnl,
                COALESCE(SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN t.pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses
            FROM prop_accounts pa
            LEFT JOIN trades t
                   ON t.account_id = pa.account_id
                  AND (t.source = 'scraper' OR t.source IS NULL)
            LEFT JOIN users u
                   ON u.telegram_user_id = pa.telegram_user_id
            WHERE pa.is_active = TRUE
            GROUP BY pa.telegram_user_id, u.first_name, pa.account_id, pa.account_type, pa.account_size
            ORDER BY total_pnl DESC
            LIMIT :lim
        """), {"lim": limit})
        rows = [dict(r) for r in result.mappings().all()]

    leaderboard = []
    for i, row in enumerate(rows):
        sz        = row.get("account_size") or 10000
        total_pnl = float(row.get("total_pnl") or 0)
        trades    = int(row.get("trade_count") or 0)
        wins      = int(row.get("wins") or 0)
        win_rate  = round(wins / trades * 100) if trades else 0
        leaderboard.append({
            "rank":         i + 1,
            "displayName":  row.get("display_name") or "Trader",
            "accountId":    row.get("account_id"),
            "accountType":  row.get("account_type"),
            "accountSize":  sz,
            "totalPnl":     round(total_pnl, 2),
            "pnlPct":       round(total_pnl / sz * 100, 2) if sz else 0,
            "tradeCount":   trades,
            "winRate":      win_rate,
        })
    return {"leaderboard": leaderboard, "total": len(leaderboard)}
