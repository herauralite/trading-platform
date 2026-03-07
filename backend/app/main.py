import os
import logging
import httpx
import asyncio
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

RAILWAY_URL = "https://trading-platform-production-70e0.up.railway.app"

INDEX_CURRENCIES = {"USD", "CNY"}
INDEX_KEYWORDS = [
    "non-farm", "nfp", "payroll", "cpi", "inflation", "pce",
    "fomc", "fed", "federal reserve", "powell", "interest rate",
    "gdp", "ism", "pmi", "unemployment", "jobless",
    "retail sales", "consumer confidence", "jolts",
]
WARN_MINUTES = 10
FRIDAY_CLOSE_HOUR_ET = 17

# ── Phase rules — corrected against FundingPips official docs ─────────────────
# Corrections vs previous version:
#   2-Step:     min_trading_days 5→3
#   2-Step Pro: profit_target 10%/5%→6%/6%, daily_loss 5%→3%, max_loss 10%→6%, min_days 5→1
#   1-Step:     daily_loss 5%→3%, max_loss 10%→6%, min_days 5→3
#   Zero:       trailing loss model, 3% daily, consistency score, 7 profitable days/30-day cycle
#   Master reward splits: weekly 60%, bi-weekly 80%, monthly 100%, on-demand 90% (+35% consistency)
PHASE_RULES = {
    "2_step_phase1": {
        "label": "2-Step Phase 1 (Student)",
        "is_master": False,
        "profit_target_pct": 8.0,
        "min_trading_days": 3,
        "daily_loss_pct": 5.0,
        "max_loss_pct": 10.0,
        "next_phase": "2_step_phase2",
        "payout_eligible": False,
    },
    "2_step_phase2": {
        "label": "2-Step Phase 2 (Practitioner)",
        "is_master": False,
        "profit_target_pct": 5.0,
        "min_trading_days": 3,
        "daily_loss_pct": 5.0,
        "max_loss_pct": 10.0,
        "next_phase": "master",
        "payout_eligible": False,
    },
    "2_step_pro_phase1": {
        "label": "2-Step Pro Phase 1 (Student)",
        "is_master": False,
        "profit_target_pct": 6.0,
        "min_trading_days": 1,
        "daily_loss_pct": 3.0,
        "max_loss_pct": 6.0,
        "next_phase": "2_step_pro_phase2",
        "payout_eligible": False,
    },
    "2_step_pro_phase2": {
        "label": "2-Step Pro Phase 2 (Practitioner)",
        "is_master": False,
        "profit_target_pct": 6.0,
        "min_trading_days": 1,
        "daily_loss_pct": 3.0,
        "max_loss_pct": 6.0,
        "next_phase": "master",
        "payout_eligible": False,
    },
    "1_step_phase1": {
        "label": "1-Step Phase 1 (Student)",
        "is_master": False,
        "profit_target_pct": 10.0,
        "min_trading_days": 3,
        "daily_loss_pct": 3.0,
        "max_loss_pct": 6.0,
        "next_phase": "master",
        "payout_eligible": False,
    },
    # Zero goes directly to Master — no evaluation phase
    "zero": {
        "label": "Zero Challenge (Master)",
        "is_master": True,
        "profit_target_pct": None,
        "min_trading_days": 7,           # 7 profitable days per 30-day cycle
        "daily_loss_pct": 3.0,
        "max_loss_pct": 5.0,             # trailing — 5% of highest equity
        "trailing_loss": True,
        "consistency_score_pct": 15.0,   # biggest day must be ≤15% of total profit
        "safety_cushion_pct": 3.0,       # first 3% profit is a safety cushion
        "next_phase": None,
        "payout_eligible": True,
        "min_payout_pct": 1.0,
        "payout_split": 0.95,
        "reward_splits": {"bi_weekly": 95},
    },
    "master": {
        "label": "Master Funded (2-Step)",
        "is_master": True,
        "profit_target_pct": None,
        "min_trading_days": 5,
        "daily_loss_pct": 5.0,
        "max_loss_pct": 10.0,
        "next_phase": None,
        "payout_eligible": True,
        "min_payout_pct": 2.0,
        "payout_split": 0.80,
        "reward_splits": {"weekly": 60, "bi_weekly": 80, "monthly": 100, "on_demand": 90},
        "consistency_score_pct": 35.0,
    },
    "2_step_master": {
        "label": "Master Funded (2-Step)",
        "is_master": True,
        "profit_target_pct": None,
        "min_trading_days": 5,
        "daily_loss_pct": 5.0,
        "max_loss_pct": 10.0,
        "next_phase": None,
        "payout_eligible": True,
        "min_payout_pct": 2.0,
        "payout_split": 0.80,
        "reward_splits": {"weekly": 60, "bi_weekly": 80, "monthly": 100, "on_demand": 90},
        "consistency_score_pct": 35.0,
    },
    "1_step_master": {
        "label": "Master Funded (1-Step)",
        "is_master": True,
        "profit_target_pct": None,
        "min_trading_days": 5,
        "daily_loss_pct": 5.0,
        "max_loss_pct": 10.0,
        "next_phase": None,
        "payout_eligible": True,
        "min_payout_pct": 2.0,
        "payout_split": 0.80,
        "reward_splits": {"weekly": 60, "bi_weekly": 80, "monthly": 100, "on_demand": 90},
        "consistency_score_pct": 35.0,
    },
    "2_step_pro_master": {
        "label": "Master Funded (2-Step Pro)",
        "is_master": True,
        "profit_target_pct": None,
        "min_trading_days": 5,
        "daily_loss_pct": 3.0,
        "max_loss_pct": 6.0,
        "next_phase": None,
        "payout_eligible": True,
        "min_payout_pct": 1.0,
        "payout_split": 0.80,
        "reward_splits": {"weekly": 80, "daily": 80},
    },
}


def get_phase_rules(account_type: str) -> dict:
    if not account_type:
        return PHASE_RULES["2_step_phase1"]
    key = account_type.lower().replace(" ", "_").replace("-", "_")
    if key in PHASE_RULES:
        return PHASE_RULES[key]
    if "zero" in key:
        return PHASE_RULES["zero"]
    if "pro" in key:
        if "master" in key:                                   return PHASE_RULES["2_step_pro_master"]
        if "phase2" in key or "phase_2" in key:              return PHASE_RULES["2_step_pro_phase2"]
        return PHASE_RULES["2_step_pro_phase1"]
    if "1_step" in key or "one_step" in key or "1step" in key:
        if "master" in key:                                   return PHASE_RULES["1_step_master"]
        return PHASE_RULES["1_step_phase1"]
    if "master" in key:                                       return PHASE_RULES["2_step_master"]
    if "phase2" in key or "phase_2" in key:                  return PHASE_RULES["2_step_phase2"]
    return PHASE_RULES["2_step_phase1"]


async def db_count_trading_days(account_id: str = None) -> int:
    from app.core.database import engine
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(
                text("SELECT COUNT(DISTINCT logged_at::date) FROM trades WHERE account_id = :a"),
                {"a": account_id}
            )
        else:
            result = await conn.execute(text("SELECT COUNT(DISTINCT logged_at::date) FROM trades"))
        return result.scalar() or 0


async def evaluate_payout_eligibility(acct_id: str, acct: dict) -> dict:
    account_type = acct.get("accountType", "")
    account_size = acct.get("accountSize") or 10000
    balance      = acct.get("balance")     or account_size
    overall      = acct.get("overallLoss") or {}
    daily        = acct.get("dailyLoss")   or {}
    rules        = get_phase_rules(account_type)

    is_master    = rules["is_master"]
    label        = rules["label"]
    profit_usd   = balance - account_size
    profit_pct   = round(profit_usd / account_size * 100, 2)
    target_pct   = rules.get("profit_target_pct")
    target_usd   = round(account_size * target_pct / 100, 2) if target_pct else None
    profit_progress = round(profit_pct / target_pct * 100, 1) if target_pct else None

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
            "pct":      min(100, profit_progress or 0),
            "passed":   profit_pct >= target_pct,
        }
    checks["min_trading_days"] = {
        "label":    f"Minimum trading days ({min_days})",
        "required": min_days,
        "current":  trading_days,
        "pct":      min(100, round(trading_days / min_days * 100)) if min_days else 100,
        "passed":   trading_days >= min_days,
    }
    checks["no_breach"] = {
        "label":   "No rule breach",
        "passed":  not breached,
        "current": f"Daily {daily_pct:.0f}% | Overall {overall_pct:.0f}%",
    }

    all_passed = all(c["passed"] for c in checks.values())

    payout_info = None
    if is_master:
        min_payout_pct = rules.get("min_payout_pct", 2.0)
        payout_split   = rules.get("payout_split", 0.80)
        reward_splits  = rules.get("reward_splits", {})
        payout_amount  = round(profit_usd * payout_split, 2) if profit_usd > 0 else 0
        payout_eligible = all_passed and profit_pct >= min_payout_pct and not breached

        is_zero = rules.get("trailing_loss", False)
        consistency_note = None
        if is_zero:
            cs_pct   = rules.get("consistency_score_pct", 15.0)
            cushion  = rules.get("safety_cushion_pct", 3.0)
            consistency_note = {
                "required_pct":       cs_pct,
                "safety_cushion_usd": account_size * cushion / 100,
                "note": f"Biggest winning day ≤{cs_pct}% of total profit. First ${account_size*cushion/100:.0f} ({cushion}%) is safety cushion.",
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
            "is_zero":          is_zero,
            "consistency_note": consistency_note,
        }

    return {
        "accountId":        acct_id,
        "accountType":      account_type,
        "label":            label,
        "is_master":        is_master,
        "balance":          balance,
        "account_size":     account_size,
        "profit_usd":       round(profit_usd, 2),
        "profit_pct":       profit_pct,
        "target_pct":       target_pct,
        "target_usd":       target_usd,
        "profit_progress":  profit_progress,
        "trading_days":     trading_days,
        "min_trading_days": min_days,
        "days_remaining":   days_remaining,
        "breached":         breached,
        "all_passed":       all_passed,
        "next_phase":       rules.get("next_phase"),
        "checks":           checks,
        "payout":           payout_info,
    }


def format_payout_status(ev: dict, short: bool = False) -> str:
    is_master = ev.get("is_master")
    label     = ev.get("label", "Unknown")
    checks    = ev.get("checks", {})
    def ck(passed): return "✅" if passed else "❌"

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
    else:
        payout        = ev.get("payout") or {}
        eligible      = payout.get("eligible")
        payout_amt    = payout.get("payout_amount", 0)
        profit_pct    = payout.get("profit_pct", 0)
        min_pct       = payout.get("min_profit_pct", 2)
        trading_days  = payout.get("trading_days", 0)
        min_days      = payout.get("min_trading_days", 5)
        split         = payout.get("payout_split", 80)
        breached      = payout.get("breached")
        reward_splits = payout.get("reward_splits", {})
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


# ── DB helpers ────────────────────────────────────────────────────────────────
async def ensure_trades_table():
    from app.core.database import engine
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
    # Add dedup constraint idempotently — safe to run on every startup
    try:
        await conn.execute(text("""
            ALTER TABLE trades
            ADD CONSTRAINT trades_dedup
            UNIQUE (account_id, symbol, direction, closed_at, pnl)
        """))
        logger.info("trades dedup constraint added")
    except Exception:
        pass  # constraint already exists — ignore

    # Add source column for existing deployments — NO DEFAULT so existing rows stay NULL.
    # We then backfill based on balance_after (the only reliable signal for old rows).
    try:
        await conn.execute(text("ALTER TABLE trades ADD COLUMN source TEXT"))
        logger.info("trades source column added")
    except Exception:
        pass  # column already exists — safe to ignore

    # Backfill source on existing rows using balance_after as the discriminator:
    # - Scraper rows never had balance_after (closed positions tab doesn't show it)
    # - Real-time rows always set balance_after from live balance at detection time
    # Run inside its own try so a backfill hiccup never blocks startup.
    try:
        await conn.execute(text("""
            UPDATE trades SET source = 'scraper'
            WHERE source IS NULL AND balance_after IS NULL;

            UPDATE trades SET source = 'realtime'
            WHERE source IS NULL AND balance_after IS NOT NULL;

            UPDATE trades SET source = 'scraper'
            WHERE source IS NULL;
        """))
        logger.info("trades source backfill complete")
    except Exception as e:
        logger.warning(f"trades source backfill skipped: {e}")

    logger.info("trades table ready")


async def db_insert_trade(trade_dict: dict):
    # Sanity guard: reject any row where |pnl| > account_size.
    # This catches the scraper fallback bug where close price (e.g. DJI30 ~47k)
    # gets stored as pnl instead of the actual profit/loss value.
    pnl_val      = trade_dict.get("pnl") or 0
    account_size = trade_dict.get("accountSize") or 10000
    if abs(pnl_val) > account_size:
        logger.warning(
            f"db_insert_trade: rejected suspicious pnl={pnl_val} for "
            f"{trade_dict.get('symbol')} (exceeds accountSize={account_size}) — "
            f"likely close price captured instead of profit"
        )
        return
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
            ON CONFLICT ON CONSTRAINT trades_dedup DO NOTHING
        """), {k: trade_dict.get(k) for k in [
            "accountId","accountType","accountSize","symbol","direction","volume",
            "openPrice","closePrice","pnl","balanceAfter","equityAfter",
            "dailyLossUsed","dailyLossLimit","overallLossUsed","overallLossLimit","closedAt","source"
        ]})


async def db_get_trades(account_id: str = None, limit: int = 50, offset: int = 0, order: str = "desc", source: str = "scraper") -> list:
    from app.core.database import engine
    order_sql = "ASC" if order.lower() == "asc" else "DESC"
    # Default to scraper-only — analytics should never show real-time duplicate rows.
    # Pass source=None or source=all to bypass filtering (admin use only).
    # Include NULL-source rows when querying scraper — they are pre-migration scraper rows
    source_clause = "" if not source or source == "all" else " AND (source = :src OR (source IS NULL AND :src = 'scraper'))"
    async with engine.connect() as conn:
        params: dict = {"l": limit, "o": offset}
        if source and source != "all":
            params["src"] = source
        if account_id:
            params["a"] = account_id
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE account_id=:a{source_clause} ORDER BY COALESCE(closed_at, logged_at) {order_sql} LIMIT :l OFFSET :o"),
                params
            )
        else:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE 1=1{source_clause} ORDER BY COALESCE(closed_at, logged_at) {order_sql} LIMIT :l OFFSET :o"),
                params
            )
        return [dict(r) for r in result.mappings().all()]


async def db_get_trade_stats(account_id: str = None) -> dict:
    """Returns total count and oldest trade date — lightweight, used for payout countdown seeding."""
    from app.core.database import engine
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(
                text("SELECT COUNT(*) as total, MIN(logged_at) as oldest FROM trades WHERE account_id=:a"),
                {"a": account_id}
            )
        else:
            result = await conn.execute(
                text("SELECT COUNT(*) as total, MIN(logged_at) as oldest FROM trades")
            )
        row = result.mappings().one_or_none()
        if not row:
            return {"total": 0, "oldest_trade_date": None}
        oldest = row["oldest"]
        if oldest and hasattr(oldest, "isoformat"):
            oldest = oldest.isoformat()
        return {"total": row["total"] or 0, "oldest_trade_date": oldest}


async def db_get_trades_today(account_id: str = None) -> list:
    from app.core.database import engine
    today = date.today()
    # Exclude realtime rows — scraper is the source of truth for all analytics/summaries
    src_filter = " AND (source = 'scraper' OR source IS NULL)"
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE account_id=:a AND logged_at::date=:t{src_filter} ORDER BY logged_at DESC"),
                {"a": account_id, "t": today}
            )
        else:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE logged_at::date=:t{src_filter} ORDER BY logged_at DESC"), {"t": today}
            )
        return [dict(r) for r in result.mappings().all()]


async def db_get_trades_for_date(target_date: str, account_id: str = None) -> list:
    from app.core.database import engine
    parsed_date = date.fromisoformat(target_date)
    # Exclude realtime rows — scraper is the source of truth for all analytics/summaries
    src_filter = " AND (source = 'scraper' OR source IS NULL)"
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE account_id=:a AND logged_at::date=:d{src_filter} ORDER BY logged_at DESC"),
                {"a": account_id, "d": parsed_date}
            )
        else:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE logged_at::date=:d{src_filter} ORDER BY logged_at DESC"), {"d": parsed_date}
            )
        return [dict(r) for r in result.mappings().all()]


async def db_get_green_streak(account_id: str = None) -> int:
    streak = 0
    check_date = date.today()
    for _ in range(30):
        rows = await db_get_trades_for_date(check_date.isoformat(), account_id)
        if not rows:
            check_date -= timedelta(days=1)
            continue
        if sum((r.get("pnl") or 0) for r in rows) > 0:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break
    return streak


def row_to_trade(row: dict) -> dict:
    closed = row.get("closed_at")
    if closed and hasattr(closed, "isoformat"): closed = closed.isoformat()
    logged = row.get("logged_at")
    if logged and hasattr(logged, "isoformat"): logged = logged.isoformat()
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
        "closedAt":         closed,
        "logged_at":        logged,
        "source":           row.get("source") or "realtime",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TaliTrade...")
    await ensure_trades_table()
    await setup_telegram_webhook()
    news_task    = asyncio.create_task(news_scheduler())
    weekend_task = asyncio.create_task(weekend_scheduler())
    summary_task = asyncio.create_task(daily_summary_scheduler())
    yield
    news_task.cancel(); weekend_task.cancel(); summary_task.cancel()
    logger.info("Shutting down...")


app = FastAPI(title="TaliTrade", version="3.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

from app.routers import auth, accounts
app.include_router(auth.router)
app.include_router(accounts.router)
from app.core.database import engine


@app.get("/health")
async def health(): return {"status": "ok"}


@app.get("/health/db")
async def health_db():
    async with engine.connect() as conn: await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str, chat_id: str = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid   = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not cid: return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": message, "parse_mode": "HTML"}
            )
    except Exception as e: logger.error(f"Telegram error: {e}")


async def setup_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: return
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": f"{RAILWAY_URL}/telegram/webhook"}
            )
            logger.info(f"Webhook: {res.json()}")
    except Exception as e: logger.error(f"Webhook setup failed: {e}")


# ── Schedulers ────────────────────────────────────────────────────────────────
daily_summary_sent = set()

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
                    await send_daily_summary(today_key)
        except Exception as e: logger.error(f"Daily summary scheduler error: {e}")
        await asyncio.sleep(60)


async def send_daily_summary(summary_date: str):
    acct_id   = "1917136" if "1917136" in account_data_store else (list(account_data_store.keys())[0] if account_data_store else "1917136")
    rows      = await db_get_trades_for_date(summary_date, account_id=acct_id)
    trades    = [row_to_trade(r) for r in rows]
    acct      = account_data_store.get(acct_id, {})
    balance   = acct.get("balance")
    daily     = acct.get("dailyLoss")   or {}
    overall   = acct.get("overallLoss") or {}
    acct_size = acct.get("accountSize") or 10000
    acct_type = (acct.get("accountType") or "").replace("_", " ").title()
    ev        = await evaluate_payout_eligibility(acct_id, acct)

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
        total_pnl = 0

    streak = await db_get_green_streak(account_id=acct_id)
    streak_line = ""
    if streak >= 3:   streak_line = f"🔥 <b>{streak}-day green streak!</b>\n"
    elif streak == 2: streak_line = "🔥 2-day green streak — stay focused.\n"
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

    d_pct = daily.get("pct")   or 0
    o_pct = overall.get("pct") or 0
    d_rem = daily.get("remaining")   or 0
    o_rem = overall.get("remaining") or 0

    balance_line = ""
    if balance:
        profit_total = balance - acct_size
        balance_line = f"💰 Balance: <b>${balance:,.2f}</b>  ({'+'if profit_total>=0 else ''}{profit_total:,.2f} overall)\n"

    if not trades:
        is_friday   = datetime.strptime(summary_date, "%Y-%m-%d").weekday() == 4
        weekend_note = "Rest up. Markets open Monday 6 PM ET 🌙" if is_friday else f"{ri(d_pct)} Daily resets midnight GMT+1 🔄\n{ri(o_pct)} Overall: {o_pct}%  (${o_rem:,.0f} remaining)"
        await send_telegram(
            f"😴 <b>Market Close — {summary_date}</b>\n{'─'*28}\n\nNo trades today.\n\n"
            f"{balance_line}{'─'*28}\n{weekend_note}\n\n{'─'*28}\n{format_payout_status(ev, short=True)}"
        )
        return

    trade_lines = "\n".join([
        f"  {'✅'if(t.get('pnl')or 0)>0 else '❌'} {t.get('symbol','?')} {t.get('direction','?')} "
        f"{'+'if(t.get('pnl')or 0)>=0 else ''}{(t.get('pnl')or 0):.2f}"
        for t in trades
    ])
    await send_telegram(
        f"{day_icon} <b>Market Close — {summary_date}</b>\n"
        f"<i>{acct_type} · ${acct_size//1000}K · {acct_id}</i>\n{'─'*28}\n\n"
        f"{streak_line}📊 <b>Today</b>\n  Net P&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b>\n"
        f"  Trades: {len(trades)}  (W:{len(wins)} L:{len(losses)})\n  Win Rate: {win_rate}%  |  PF: {pf}\n"
        f"  Best: {best_t.get('symbol','?')} +{best:.2f}  |  Worst: {worst_t.get('symbol','?')} {worst:.2f}\n"
        f"  Avg W: +{avg_win:.2f}  |  Avg L: {avg_loss:.2f}\n\n<b>Trades</b>\n{trade_lines}\n\n"
        f"{'─'*28}\n<b>Risk Tomorrow</b>\n{ri(d_pct)} Daily: {d_pct}%  {rb(d_pct)}  (${d_rem:,.0f} left)\n"
        f"{ri(o_pct)} Overall: {o_pct}%  {rb(o_pct)}  (${o_rem:,.0f} left)\n\n"
        f"{balance_line}{'─'*28}\n{format_payout_status(ev, short=True)}\n\nSee you tomorrow 🎯"
    )
    logger.info(f"Daily summary sent: {summary_date} | P&L: {total_pnl:.2f}")


weekend_alerted = set()

async def weekend_scheduler():
    logger.info("Weekend scheduler started")
    while True:
        try:
            now_utc   = datetime.now(timezone.utc)
            et_offset = -4 if 3 <= now_utc.month <= 11 else -5
            now_et    = now_utc + timedelta(hours=et_offset)
            if now_et.weekday() == 4:
                today_key = now_et.strftime("%Y-%m-%d")
                for warn_hour, warn_min, label, icon in [(16,0,"1 HOUR","⚠️"),(16,30,"30 MINUTES","🔴"),(16,45,"15 MINUTES","🚨")]:
                    key = f"{today_key}_{warn_hour}_{warn_min}"
                    if key in weekend_alerted: continue
                    target = now_et.replace(hour=warn_hour, minute=warn_min, second=0, microsecond=0)
                    if abs((now_et - target).total_seconds()) <= 60:
                        weekend_alerted.add(key)
                        open_accts = [aid for aid, a in account_data_store.items() if a.get("hasPositions")]
                        pos_warn = (f"\n🔴 <b>OPEN POSITIONS!</b>\nAccounts: {', '.join(open_accts)}\nProfits will NOT count.\n") if open_accts else ""
                        await send_telegram(
                            f"{icon} <b>MARKET CLOSES IN {label}</b>\n{'─'*28}\n🗓 Friday close: <b>5:00 PM ET</b>\n"
                            f"📊 Affects: DJI30, NAS100, SP500, Forex, Gold\n{pos_warn}{'─'*28}\n"
                            f"⚠️ Holding over weekend <b>not permitted</b>.\nProfits <b>won't count</b> — close before 5 PM ET."
                        )
        except Exception as e: logger.error(f"Weekend scheduler error: {e}")
        await asyncio.sleep(60)


news_cache = []; news_alerted = set(); news_last_fetch = None

def is_index_relevant(event: dict) -> bool:
    currency = (event.get("country") or "").upper()
    title    = (event.get("title")   or "").lower()
    impact   = (event.get("impact")  or "").lower()
    if impact not in ["high", "red", "3"]: return False
    if currency in INDEX_CURRENCIES: return True
    return any(kw in title for kw in INDEX_KEYWORDS)

async def fetch_news_calendar() -> list:
    global news_cache, news_last_fetch
    now = datetime.now(timezone.utc)
    if news_last_fetch and (now - news_last_fetch).seconds < 3600 and news_cache: return news_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers={"User-Agent":"Mozilla/5.0"})
            if res.status_code == 200:
                news_cache = res.json(); news_last_fetch = now
                logger.info(f"News: {len(news_cache)} events")
                return news_cache
    except Exception as e: logger.error(f"News fetch error: {e}")
    return news_cache

def parse_event_time(event: dict):
    try:
        date_str = event.get("date",""); time_str = event.get("time","")
        if not date_str or not time_str or time_str.lower() in ["","all day","tentative"]: return None
        dt_date  = datetime.strptime(date_str, "%m-%d-%Y").date()
        time_str = time_str.strip().lower()
        dt_time  = (datetime.strptime(time_str, "%I:%M%p") if ":" in time_str else datetime.strptime(time_str, "%I%p")).time()
        naive  = datetime.combine(dt_date, dt_time)
        offset = -4 if 3 <= dt_date.month <= 11 else -5
        return naive.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    except Exception: return None

async def news_scheduler():
    logger.info("News scheduler started")
    while True:
        try:
            events = await fetch_news_calendar(); now = datetime.now(timezone.utc)
            for event in events:
                if not is_index_relevant(event): continue
                et = parse_event_time(event)
                if not et: continue
                mins = (et - now).total_seconds() / 60
                key  = f"{event.get('title','')}_{et.isoformat()}"
                if WARN_MINUTES-1 <= mins <= WARN_MINUTES+1 and key not in news_alerted:
                    news_alerted.add(key); await send_news_alert(event, et, round(mins))
                key30 = f"30min_{key}"; tl = (event.get("title") or "").lower()
                if any(w in tl for w in ["speech","fomc","powell","fed chair","testimony"]) and 29 <= mins <= 31 and key30 not in news_alerted:
                    news_alerted.add(key30); await send_news_alert(event, et, round(mins))
        except Exception as e: logger.error(f"News scheduler error: {e}")
        await asyncio.sleep(60)

async def send_news_alert(event: dict, event_time: datetime, minutes: int):
    title    = event.get("title","Unknown"); currency = (event.get("country") or "").upper()
    forecast = event.get("forecast","");    previous = event.get("previous","")
    et_offset = -4 if 3 <= event_time.month <= 11 else -5
    et_time   = event_time + timedelta(hours=et_offset)
    tl = title.lower(); guidance = ""
    if any(w in tl for w in ["fomc","fed","powell","interest rate"]): guidance = "⚡ <b>Fed event — expect high volatility.</b>\n"
    elif any(w in tl for w in ["non-farm","nfp","payroll"]):          guidance = "⚡ <b>NFP — biggest mover for indices.</b>\n"
    elif any(w in tl for w in ["cpi","inflation","pce"]):             guidance = "⚡ <b>Inflation data — rate expectations impact.</b>\n"
    forecast_line = f"Forecast: <b>{forecast}</b> | Previous: {previous}\n" if forecast else ""
    await send_telegram(
        f"🗞 <b>HIGH-IMPACT NEWS IN {minutes} MIN</b>\n{'─'*28}\n📌 <b>{title}</b>\n🌍 Currency: <b>{currency}</b>\n"
        f"🕐 Time: <b>{et_time.strftime('%I:%M %p ET')}</b>\n📊 Affects: <b>DJI30, NAS100, SP500</b>\n"
        f"{forecast_line}{'─'*28}\n{guidance}⚠️ No trades within <b>5 min before or after</b> this event."
    )


# ── In-memory live state ──────────────────────────────────────────────────────
account_data_store: dict = {}


# ── Telegram webhook ──────────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body    = await request.json()
    message = body.get("message", {})
    text    = message.get("text", "").strip().lower()
    chat_id = str(message.get("chat", {}).get("id", ""))

    cmds = {"/status": handle_status, "/today": handle_today, "/journal": handle_journal, "/news": handle_news}
    for cmd, fn in cmds.items():
        if text in [cmd, f"{cmd}@talitrade_bot"]:
            await fn(chat_id); return {"ok": True}

    if text in ["/payout",  "/payout@talitrade_bot"]:  await handle_payout(chat_id)
    elif text in ["/summary","/summary@talitrade_bot"]: await send_daily_summary(date.today().isoformat())
    elif text in ["/help",   "/help@talitrade_bot"]:
        await send_telegram(
            "🤖 <b>TaliTrade Commands</b>\n\n"
            "/status  — Live risk snapshot\n/today   — Today's trades & P&L\n"
            "/journal — Last 10 trades\n/news    — Upcoming high-impact news\n"
            "/payout  — Payout eligibility check\n/summary — Today's market-close recap\n"
            "/help    — This message", chat_id=chat_id
        )
    return {"ok": True}


async def handle_payout(chat_id: str):
    acct_id = list(account_data_store.keys())[0] if account_data_store else "1917136"; acct = account_data_store.get(acct_id)
    if not acct:
        await send_telegram("📡 No data — open FundingPips in your browser first.", chat_id=chat_id); return
    ev = await evaluate_payout_eligibility(acct_id, acct)
    await send_telegram(f"💸 <b>Payout Check — {acct_id}</b>\n{'─'*28}\n\n{format_payout_status(ev, short=False)}", chat_id=chat_id)


async def handle_news(chat_id: str):
    events = await fetch_news_calendar(); now = datetime.now(timezone.utc); upcoming = []
    for event in events:
        if not is_index_relevant(event): continue
        et = parse_event_time(event)
        if not et: continue
        mins = (et - now).total_seconds() / 60
        if 0 < mins < 480: upcoming.append((mins, event, et))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        await send_telegram("📅 No high-impact news in the next 8 hours.", chat_id=chat_id); return
    lines = []
    for mins, event, et in upcoming[:8]:
        et_offset = -4 if 3 <= et.month <= 11 else -5; et_time = et + timedelta(hours=et_offset)
        when = f"in {round(mins)}m" if mins < 60 else f"in {round(mins/60,1)}h"
        lines.append(f"🔴 <b>{event.get('title','?')}</b> — {et_time.strftime('%I:%M %p')} ET ({when})")
    await send_telegram(
        f"📅 <b>Upcoming High-Impact News</b>\n<i>DJI30, NAS100, SP500</i>\n{'─'*28}\n\n"
        + "\n".join(lines) + f"\n\n{'─'*28}\n⚠️ No trades within 5 min before/after each event.", chat_id=chat_id
    )


async def handle_status(chat_id: str):
    if not account_data_store:
        await send_telegram("📡 No data — open FundingPips in your browser first.", chat_id=chat_id); return
    acct_id   = "1917136" if "1917136" in account_data_store else list(account_data_store.keys())[0]
    acct      = account_data_store[acct_id]
    balance   = acct.get("balance") or 0; equity = acct.get("equity") or 0; profit = acct.get("profit") or 0
    risk      = acct.get("riskPerTradeIdea") or {}; daily = acct.get("dailyLoss") or {}; overall = acct.get("overallLoss") or {}
    acct_type = acct.get("accountType","unknown"); acct_size = acct.get("accountSize",10000)
    last      = (acct.get("last_updated") or "")[:19].replace("T"," ")
    def bar(pct): f=round((pct or 0)/10); return "█"*f+"░"*(10-f)
    def icon(pct):
        if pct is None: return "⚪"
        if pct >= 90: return "🚨"
        if pct >= 75: return "🔴"
        if pct >= 50: return "⚠️"
        return "✅"
    risk_line = ""
    if risk.get("applicable"):
        risk_line = (f"{icon(risk.get('pct'))} <b>Trade Idea Risk</b>  {risk.get('pct',0)}%\n"
                     f"  {bar(risk.get('pct',0))}  ${risk.get('combined',0):.0f} / ${risk.get('limit',300):.0f}\n"
                     f"  Remaining: <b>${risk.get('remaining',300):.0f}</b>\n\n")
    ev = await evaluate_payout_eligibility(acct_id, acct)
    await send_telegram(
        f"📊 <b>TaliTrade — {acct_id}</b>\n<i>{acct_type} | ${acct_size/1000:.0f}K</i>\n{'─'*28}\n\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n📈 Equity: <b>${equity:.2f}</b>\n📉 P&L: <b>{'+'if profit>=0 else ''}{profit:.2f}</b>\n\n"
        f"{'─'*28}\n{risk_line}"
        f"{icon(daily.get('pct'))} <b>Daily Loss</b>  {daily.get('pct',0)}%\n"
        f"  {bar(daily.get('pct',0))}  ${daily.get('used',0):.0f} / ${daily.get('limit',500):.0f}\n"
        f"  Remaining: <b>${daily.get('remaining',500):.0f}</b>\n\n"
        f"{icon(overall.get('pct'))} <b>Overall Loss</b>  {overall.get('pct',0)}%\n"
        f"  {bar(overall.get('pct',0))}  ${overall.get('used',0):.0f} / ${overall.get('limit',1000):.0f}\n"
        f"  Remaining: <b>${overall.get('remaining',1000):.0f}</b>\n\n"
        f"{'─'*28}\n{format_payout_status(ev, short=True)}\n\n🕐 {last} UTC", chat_id=chat_id
    )


async def handle_today(chat_id: str):
    today        = date.today().isoformat()
    rows         = await db_get_trades_today(account_id="1917136")
    today_trades = [row_to_trade(r) for r in rows]
    if not today_trades:
        await send_telegram(f"📅 No trades logged today ({today}).", chat_id=chat_id); return
    total_pnl = sum(t.get("pnl") or 0 for t in today_trades)
    wins      = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    win_rate  = round(len(wins) / len(today_trades) * 100)
    lines = [f"{'✅'if(t.get('pnl')or 0)>0 else '❌'} {t.get('symbol','?')} {t.get('direction','?')} "
             f"<b>{'+'if(t.get('pnl')or 0)>=0 else ''}{(t.get('pnl')or 0):.2f}</b> @ {(t.get('closedAt') or '')[11:16]}"
             for t in today_trades]
    await send_telegram(
        f"📅 <b>Today — {today}</b>\n{'─'*28}\n\n" + "\n".join(lines)
        + f"\n\n{'─'*28}\nP&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> | {len(today_trades)} trades | WR: {win_rate}%",
        chat_id=chat_id
    )


async def handle_journal(chat_id: str):
    rows   = await db_get_trades(account_id="1917136", limit=10)
    recent = [row_to_trade(r) for r in rows]
    if not recent:
        await send_telegram("📒 No trades in journal yet.", chat_id=chat_id); return
    total_pnl = sum(t.get("pnl") or 0 for t in recent)
    wins      = len([t for t in recent if (t.get("pnl") or 0) > 0])
    lines = [f"{'✅'if(t.get('pnl')or 0)>0 else '❌'} <b>{t.get('symbol','?')}</b> {t.get('direction','?')} "
             f"{'+'if(t.get('pnl')or 0)>=0 else ''}{(t.get('pnl')or 0):.2f} | {(t.get('closedAt') or '')[:10]}"
             for t in recent]
    await send_telegram(
        f"📒 <b>Last {len(recent)} Trades</b>\n{'─'*28}\n\n" + "\n".join(lines)
        + f"\n\n{'─'*28}\nP&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> | WR: {round(wins/len(recent)*100)}%",
        chat_id=chat_id
    )


# ── Extension endpoints ───────────────────────────────────────────────────────
class ExtensionData(BaseModel):
    profit: float | None = None; balance: float | None = None; equity: float | None = None
    accountId: str | None = None; accountType: str | None = None; accountSize: int | None = None
    accountLabel: str | None = None; isMaster: bool = False; hasPositions: bool = False
    openPositionCount: int = 0; positions: list = []; riskPerTradeIdea: dict | None = None
    dailyLoss: dict | None = None; overallLoss: dict | None = None; alerts: list = []
    closedTrades: list = []  # real-time detected closes — for Telegram alert only, NOT written to DB
    timestamp: str | None = None; url: str | None = None


@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    account_id = data.accountId or "unknown"
    prev       = account_data_store.get(account_id, {})
    prev_alerts = {a.get("type"): a.get("level") for a in (prev.get("alerts") or [])}
    account_data_store[account_id] = {**data.dict(), "last_updated": datetime.utcnow().isoformat()}
    for alert in data.alerts:
        # Only send Telegram if this alert type/level is new vs previous poll
        if prev_alerts.get(alert.get("type")) != alert.get("level"):
            await send_telegram(alert.get("message","") + f"\n\n<i>Account: {account_id}</i>")
    # Real-time trade close notifications — arrive within 5s of close detection
    # These are NOT written to DB (scraper handles DB persistence with correct close times)
    for ct in data.closedTrades:
        pnl = ct.get("pnl") or 0
        icon = "✅" if pnl > 0 else "❌"
        daily_pct   = round((ct.get("dailyLossUsed")  or 0) / (ct.get("dailyLossLimit")  or 500)  * 100)
        overall_pct = round((ct.get("overallLossUsed") or 0) / (ct.get("overallLossLimit") or 1000) * 100)
        bal = ct.get("balanceAfter")
        bal_line = f"Balance: ${bal:,.2f}\n" if bal else ""
        await send_telegram(
            f"{icon} <b>Trade Closed</b>\nAccount: {account_id}\n"
            f"{ct.get('symbol','?')} {ct.get('direction','?')} | "
            f"<b>{'+'if pnl>=0 else ''}{pnl:.2f}</b>\n"
            f"{bal_line}Daily: {daily_pct}% | Overall: {overall_pct}%"
        )
    prev_profit = prev.get("profit"); curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        if prev_profit - curr_profit >= 10:
            await send_telegram(f"📉 <b>Profit Drop</b>\nAccount: {account_id}\n${prev_profit:.2f} → ${curr_profit:.2f}  (-${prev_profit-curr_profit:.2f})")
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(f"✅ <b>Position in Profit!</b>\nAccount: {account_id} | ${curr_profit:.2f}")
    risk = data.riskPerTradeIdea or {}; daily = data.dailyLoss or {}; overall = data.overallLoss or {}
    return {
        "status":"ok","account":account_id,"balance":data.balance,"equity":data.equity,
        "tradeRisk":   {"used":risk.get("combined"),   "remaining":risk.get("remaining"),   "pct":risk.get("pct")},
        "dailyLoss":   {"used":daily.get("used"),       "remaining":daily.get("remaining"),   "pct":daily.get("pct")},
        "overallLoss": {"used":overall.get("used"),     "remaining":overall.get("remaining"), "pct":overall.get("pct")},
        "alerts_fired": len(data.alerts),
    }


class TradeData(BaseModel):
    accountId: str | None = None; accountType: str | None = None; accountSize: int | None = None
    symbol: str | None = None; direction: str | None = None; volume: float | None = None
    openPrice: float | None = None; closePrice: float | None = None; pnl: float | None = None
    balanceAfter: float | None = None; equityAfter: float | None = None
    dailyLossUsed: float | None = None; dailyLossLimit: float | None = None
    overallLossUsed: float | None = None; overallLossLimit: float | None = None; closedAt: str | None = None
    source: str | None = "realtime"  # 'scraper' | 'realtime'


@app.post("/extension/trade")
async def log_trade(trade: TradeData):
    # DB-only endpoint — no Telegram here.
    # Real-time close alerts are sent via /extension/data (closedTrades field)
    # which fires within 5s of close detection with full balance context.
    # This endpoint is called by the scraper (60s delay) — too late for useful alerts.
    await db_insert_trade(trade.dict())
    return {"status":"ok","persisted":True}


@app.get("/extension/journal")
async def get_journal(account_id: str = None, limit: int = 50, offset: int = 0, order: str = "desc", source: str = "scraper"):
    rows = await db_get_trades(account_id=account_id, limit=limit, offset=offset, order=order, source=source)
    return {"trades": [row_to_trade(r) for r in rows], "total": len(rows), "offset": offset, "limit": limit}


@app.get("/extension/journal/stats")
async def get_journal_stats(account_id: str = None):
    """Lightweight endpoint — returns total trade count and oldest trade date for payout countdown seeding."""
    return await db_get_trade_stats(account_id=account_id)


# Alias — content.js was posting to /journal/trade instead of /extension/trade.
# Keep both alive so any existing extension installs don't silently drop trades.
@app.post("/journal/trade")
async def log_trade_alias(trade: TradeData):
    return await log_trade(trade)


@app.get("/extension/status")
async def extension_status():
    return {"accounts": account_data_store, "count": len(account_data_store)}


@app.get("/extension/news")
async def get_news():
    events = await fetch_news_calendar(); now = datetime.now(timezone.utc); upcoming = []
    for event in events:
        if not is_index_relevant(event): continue
        et = parse_event_time(event)
        if not et: continue
        mins = (et - now).total_seconds() / 60
        if -60 < mins < 480:
            et_offset = -4 if 3 <= et.month <= 11 else -5; et_time = et + timedelta(hours=et_offset)
            upcoming.append({
                "title": event.get("title"), "currency": event.get("country"),
                "time_et": et_time.strftime("%I:%M %p ET"), "time_utc": et.isoformat(),
                "minutes_until": round(mins), "forecast": event.get("forecast"), "previous": event.get("previous"),
            })
    upcoming.sort(key=lambda x: x["minutes_until"])
    return {"events": upcoming[:10]}


@app.get("/extension/payout")
async def get_payout(account_id: str = "1917136"):
    acct = account_data_store.get(account_id, {})
    return await evaluate_payout_eligibility(account_id, acct)


# ── Test endpoints ────────────────────────────────────────────────────────────
@app.get("/test/telegram")
async def test_telegram():
    await send_telegram("🚀 <b>TaliTrade v3.2 live!</b>\nPayout tracker active ✅")
    return {"status":"sent"}

@app.get("/test/summary")
async def test_summary():
    today_str = date.today().isoformat(); await send_daily_summary(today_str)
    return {"status":"sent","date":today_str}

@app.get("/test/payout")
async def test_payout():
    acct_id = "1917136"; acct = account_data_store.get(acct_id, {})
    return await evaluate_payout_eligibility(acct_id, acct)

@app.get("/test/news")
async def test_news():
    await send_telegram(
        "🗞 <b>HIGH-IMPACT NEWS IN 10 MIN [TEST]</b>\n──────────────────────────────\n"
        "📌 <b>US Non-Farm Payrolls</b>\n🌍 Currency: <b>USD</b>\n🕐 Time: <b>08:30 AM ET</b>\n"
        "📊 Affects: <b>DJI30, NAS100, SP500</b>\n──────────────────────────────\n"
        "⚡ <b>NFP — biggest mover for indices.</b>\n⚠️ No trades within 5 min before/after this event."
    )
    return {"status":"sent"}

@app.get("/test/weekend")
async def test_weekend():
    await send_telegram(
        "⚠️ <b>MARKET CLOSES IN 1 HOUR [TEST]</b>\n──────────────────────────────\n"
        "🗓 Friday close: <b>5:00 PM ET</b>\n📊 Affects: DJI30, NAS100, SP500, Forex, Gold\n"
        "──────────────────────────────\n⚠️ Holding over weekend is <b>not permitted</b>.\n"
        "Profits will <b>not count</b> — close before 5 PM ET."
    )
    return {"status":"sent"}

@app.get("/test/db")
async def test_db():
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM trades"))).scalar()
    return {"status":"ok","trades_in_db":count}


@app.get("/admin/dedup-trades/preview")
async def dedup_trades_preview():
    """Preview what /admin/dedup-trades DELETE would remove — no data modified."""
    from app.core.database import engine
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT id, account_id, symbol, direction, pnl, closed_at, source, balance_after
            FROM trades
            WHERE source = 'realtime'
            ORDER BY closed_at DESC
            LIMIT 50
        """))
        rows = [dict(r) for r in result.mappings().all()]
        count = await conn.execute(text("SELECT COUNT(*) FROM trades WHERE source = 'realtime'"))
        total = count.scalar()
    return {"total_realtime_rows": total, "sample": rows}


@app.delete("/admin/dedup-trades")
async def dedup_trades():
    """Removes realtime-tagged duplicate rows from the trades table.
    Safe to run multiple times — only deletes rows explicitly tagged source='realtime'.
    Pre-migration scraper rows (source IS NULL) are never touched.
    After running this, analytics shows only closed-position scraper data."""
    from app.core.database import engine
    async with engine.begin() as conn:
        # First: show a preview of what will be deleted
        preview = await conn.execute(text("""
            SELECT COUNT(*) as cnt, source,
                   MIN(closed_at) as earliest, MAX(closed_at) as latest
            FROM trades
            WHERE source = 'realtime'
            GROUP BY source
        """))
        preview_rows = [dict(r) for r in preview.mappings().all()]
        logger.info(f"dedup-trades preview: {preview_rows}")

        result = await conn.execute(text("""
            DELETE FROM trades
            WHERE source = 'realtime'
            RETURNING id, account_id, symbol, direction, pnl, closed_at
        """))
        deleted = [dict(r) for r in result.mappings().all()]
    logger.info(f"dedup-trades: removed {len(deleted)} realtime rows")
    return {"status": "ok", "deleted_count": len(deleted), "preview": preview_rows, "deleted_rows": deleted}


@app.delete("/admin/purge-corrupt-trades")
async def purge_corrupt_trades():
    """One-time cleanup: deletes rows where |pnl| > account_size.
    These are rows where the scraper captured the close price instead of profit.
    Run once after deploying the scraper fix."""
    from app.core.database import engine
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            DELETE FROM trades
            WHERE ABS(pnl) > account_size
            RETURNING id, account_id, symbol, pnl, account_size
        """))
        deleted = [dict(r) for r in result.mappings().all()]
    logger.info(f"purge-corrupt-trades: removed {len(deleted)} rows")
    return {"status": "ok", "deleted_count": len(deleted), "deleted_rows": deleted}
