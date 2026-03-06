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

# ── News config ───────────────────────────────────────────────────────────────
INDEX_CURRENCIES = {"USD", "CNY"}
INDEX_KEYWORDS = [
    "non-farm", "nfp", "payroll", "cpi", "inflation", "pce",
    "fomc", "fed", "federal reserve", "powell", "interest rate",
    "gdp", "ism", "pmi", "unemployment", "jobless",
    "retail sales", "consumer confidence", "jolts",
]
WARN_MINUTES = 10

FRIDAY_WARN_HOURS_ET = [16, 16, 16]
FRIDAY_WARN_MINS_ET  = [0,  30, 45]
FRIDAY_CLOSE_HOUR_ET = 17


# ── DB helpers ────────────────────────────────────────────────────────────────
async def ensure_trades_table():
    """Create trades table if it doesn't exist (safe to run every startup)."""
    from app.core.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trades (
                id          SERIAL PRIMARY KEY,
                account_id  TEXT,
                account_type TEXT,
                account_size INTEGER,
                symbol      TEXT,
                direction   TEXT,
                volume      FLOAT,
                open_price  FLOAT,
                close_price FLOAT,
                pnl         FLOAT,
                balance_after FLOAT,
                equity_after  FLOAT,
                daily_loss_used  FLOAT,
                daily_loss_limit FLOAT,
                overall_loss_used  FLOAT,
                overall_loss_limit FLOAT,
                closed_at   TIMESTAMPTZ,
                logged_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """))
    logger.info("trades table ready")


async def db_insert_trade(trade_dict: dict):
    from app.core.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO trades (
                account_id, account_type, account_size,
                symbol, direction, volume, open_price, close_price, pnl,
                balance_after, equity_after,
                daily_loss_used, daily_loss_limit,
                overall_loss_used, overall_loss_limit,
                closed_at
            ) VALUES (
                :accountId, :accountType, :accountSize,
                :symbol, :direction, :volume, :openPrice, :closePrice, :pnl,
                :balanceAfter, :equityAfter,
                :dailyLossUsed, :dailyLossLimit,
                :overallLossUsed, :overallLossLimit,
                :closedAt
            )
        """), {
            "accountId":        trade_dict.get("accountId"),
            "accountType":      trade_dict.get("accountType"),
            "accountSize":      trade_dict.get("accountSize"),
            "symbol":           trade_dict.get("symbol"),
            "direction":        trade_dict.get("direction"),
            "volume":           trade_dict.get("volume"),
            "openPrice":        trade_dict.get("openPrice"),
            "closePrice":       trade_dict.get("closePrice"),
            "pnl":              trade_dict.get("pnl"),
            "balanceAfter":     trade_dict.get("balanceAfter"),
            "equityAfter":      trade_dict.get("equityAfter"),
            "dailyLossUsed":    trade_dict.get("dailyLossUsed"),
            "dailyLossLimit":   trade_dict.get("dailyLossLimit"),
            "overallLossUsed":  trade_dict.get("overallLossUsed"),
            "overallLossLimit": trade_dict.get("overallLossLimit"),
            "closedAt":         trade_dict.get("closedAt"),
        })


async def db_get_trades(account_id: str = None, limit: int = 50) -> list:
    from app.core.database import engine
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(text("""
                SELECT * FROM trades
                WHERE account_id = :account_id
                ORDER BY logged_at DESC
                LIMIT :limit
            """), {"account_id": account_id, "limit": limit})
        else:
            result = await conn.execute(text("""
                SELECT * FROM trades
                ORDER BY logged_at DESC
                LIMIT :limit
            """), {"limit": limit})
        rows = result.mappings().all()
        return [dict(r) for r in rows]


async def db_get_trades_today(account_id: str = None) -> list:
    from app.core.database import engine
    today = date.today().isoformat()
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(text("""
                SELECT * FROM trades
                WHERE account_id = :account_id
                  AND closed_at::date = :today
                ORDER BY logged_at DESC
            """), {"account_id": account_id, "today": today})
        else:
            result = await conn.execute(text("""
                SELECT * FROM trades
                WHERE closed_at::date = :today
                ORDER BY logged_at DESC
            """), {"today": today})
        rows = result.mappings().all()
        return [dict(r) for r in rows]


def row_to_trade(row: dict) -> dict:
    """Normalise DB row to the same shape the frontend/telegram expects."""
    closed = row.get("closed_at")
    if closed and hasattr(closed, "isoformat"):
        closed = closed.isoformat()
    logged = row.get("logged_at")
    if logged and hasattr(logged, "isoformat"):
        logged = logged.isoformat()

    pnl = row.get("pnl") or 0
    daily_used  = row.get("daily_loss_used")  or 0
    daily_limit = row.get("daily_loss_limit") or 500
    daily_pct   = round(daily_used / daily_limit * 100) if daily_limit else 0

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
        "dailyPct":         daily_pct,
        "closedAt":         closed,
        "logged_at":        logged,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TaliTrade...")
    await ensure_trades_table()
    await setup_telegram_webhook()
    news_task    = asyncio.create_task(news_scheduler())
    weekend_task = asyncio.create_task(weekend_scheduler())
    yield
    news_task.cancel()
    weekend_task.cancel()
    logger.info("Shutting down...")


app = FastAPI(title="TaliTrade", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import auth, accounts
app.include_router(auth.router)
app.include_router(accounts.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


from app.core.database import engine

@app.get("/health/db")
async def health_db():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str, chat_id: str = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not cid:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": message, "parse_mode": "HTML"}
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
                json={"url": f"{RAILWAY_URL}/telegram/webhook"}
            )
            logger.info(f"Webhook: {res.json()}")
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")


# ── Weekend scheduler ─────────────────────────────────────────────────────────
weekend_alerted = set()

async def weekend_scheduler():
    logger.info("Weekend scheduler started")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            et_offset = -4 if 3 <= now_utc.month <= 11 else -5
            now_et = now_utc + timedelta(hours=et_offset)
            if now_et.weekday() == 4:
                today_key = now_et.strftime("%Y-%m-%d")
                warn_times = [
                    (16, 0,  "1 HOUR",    "⚠️"),
                    (16, 30, "30 MINUTES", "🔴"),
                    (16, 45, "15 MINUTES", "🚨"),
                ]
                for warn_hour, warn_min, label, icon in warn_times:
                    alert_key = f"{today_key}_{warn_hour}_{warn_min}"
                    if alert_key in weekend_alerted:
                        continue
                    target = now_et.replace(hour=warn_hour, minute=warn_min, second=0, microsecond=0)
                    if abs((now_et - target).total_seconds()) <= 60:
                        weekend_alerted.add(alert_key)
                        open_accounts = [
                            acct_id for acct_id, acct in account_data_store.items()
                            if acct.get("hasPositions")
                        ]
                        position_warning = ""
                        if open_accounts:
                            position_warning = (
                                f"\n🔴 <b>YOU HAVE OPEN POSITIONS!</b>\n"
                                f"Accounts: {', '.join(open_accounts)}\n"
                                f"Any profits will NOT count.\n"
                            )
                        msg = (
                            f"{icon} <b>MARKET CLOSES IN {label}</b>\n"
                            f"{'─'*28}\n"
                            f"🗓 Friday close: <b>5:00 PM ET</b>\n"
                            f"📊 Affects: DJI30, NAS100, SP500, Forex, Gold\n"
                            f"{position_warning}"
                            f"{'─'*28}\n"
                            f"⚠️ Holding over weekend is <b>not permitted</b>.\n"
                            f"Profits will <b>not count</b> — close before 5 PM ET."
                        )
                        await send_telegram(msg)
        except Exception as e:
            logger.error(f"Weekend scheduler error: {e}")
        await asyncio.sleep(60)


# ── News calendar ─────────────────────────────────────────────────────────────
news_cache = []
news_alerted = set()
news_last_fetch = None


def is_index_relevant(event: dict) -> bool:
    currency = (event.get("country") or "").upper()
    title    = (event.get("title")   or "").lower()
    impact   = (event.get("impact")  or "").lower()
    if impact not in ["high", "red", "3"]:
        return False
    if currency in INDEX_CURRENCIES:
        return True
    if any(kw in title for kw in INDEX_KEYWORDS):
        return True
    return False


async def fetch_news_calendar() -> list:
    global news_cache, news_last_fetch
    now = datetime.now(timezone.utc)
    if news_last_fetch and (now - news_last_fetch).seconds < 3600 and news_cache:
        return news_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if res.status_code == 200:
                news_cache = res.json()
                news_last_fetch = now
                logger.info(f"News: fetched {len(news_cache)} events")
                return news_cache
    except Exception as e:
        logger.error(f"News fetch error: {e}")
    return news_cache


def parse_event_time(event: dict):
    try:
        date_str = event.get("date", "")
        time_str = event.get("time", "")
        if not date_str or not time_str or time_str.lower() in ["", "all day", "tentative"]:
            return None
        dt_date = datetime.strptime(date_str, "%m-%d-%Y").date()
        time_str = time_str.strip().lower()
        dt_time = (datetime.strptime(time_str, "%I:%M%p") if ":" in time_str
                   else datetime.strptime(time_str, "%I%p")).time()
        naive = datetime.combine(dt_date, dt_time)
        offset = -4 if 3 <= dt_date.month <= 11 else -5
        return naive.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    except Exception:
        return None


async def news_scheduler():
    logger.info("News scheduler started")
    while True:
        try:
            events = await fetch_news_calendar()
            now = datetime.now(timezone.utc)
            for event in events:
                if not is_index_relevant(event):
                    continue
                event_time = parse_event_time(event)
                if not event_time:
                    continue
                minutes_until = (event_time - now).total_seconds() / 60
                key = f"{event.get('title','')}_{event_time.isoformat()}"
                if WARN_MINUTES - 1 <= minutes_until <= WARN_MINUTES + 1 and key not in news_alerted:
                    news_alerted.add(key)
                    await send_news_alert(event, event_time, round(minutes_until))
                key30 = f"30min_{key}"
                title_lower = (event.get("title") or "").lower()
                is_speech = any(w in title_lower for w in ["speech","fomc","powell","fed chair","testimony"])
                if is_speech and 29 <= minutes_until <= 31 and key30 not in news_alerted:
                    news_alerted.add(key30)
                    await send_news_alert(event, event_time, round(minutes_until))
        except Exception as e:
            logger.error(f"News scheduler error: {e}")
        await asyncio.sleep(60)


async def send_news_alert(event: dict, event_time: datetime, minutes: int):
    title    = event.get("title", "Unknown Event")
    currency = (event.get("country") or "").upper()
    forecast = event.get("forecast", "")
    previous = event.get("previous", "")
    et_offset = -4 if 3 <= event_time.month <= 11 else -5
    et_time = event_time + timedelta(hours=et_offset)
    time_str = et_time.strftime("%I:%M %p ET")
    tl = title.lower()
    guidance = ""
    if any(w in tl for w in ["fomc","fed","powell","interest rate"]):
        guidance = "⚡ <b>Fed event — expect high volatility.</b>\n"
    elif any(w in tl for w in ["non-farm","nfp","payroll"]):
        guidance = "⚡ <b>NFP — biggest mover for indices.</b>\n"
    elif any(w in tl for w in ["cpi","inflation","pce"]):
        guidance = "⚡ <b>Inflation data — major rate expectations impact.</b>\n"
    forecast_line = f"Forecast: <b>{forecast}</b> | Previous: {previous}\n" if forecast else ""
    await send_telegram(
        f"🗞 <b>HIGH-IMPACT NEWS IN {minutes} MIN</b>\n{'─'*28}\n"
        f"📌 <b>{title}</b>\n"
        f"🌍 Currency: <b>{currency}</b>\n"
        f"🕐 Time: <b>{time_str}</b>\n"
        f"📊 Affects: <b>DJI30, NAS100, SP500</b>\n"
        f"{forecast_line}{'─'*28}\n"
        f"{guidance}"
        f"⚠️ No trades within <b>5 min before or after</b> this event."
    )
    logger.info(f"News alert: {title} in {minutes} min")


# ── In-memory store (account state only, NOT trades) ─────────────────────────
account_data_store: dict = {}


# ── Telegram webhook ──────────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body    = await request.json()
    message = body.get("message", {})
    text    = message.get("text", "").strip().lower()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if text in ["/status", "/status@talitrade_bot"]:
        await handle_status(chat_id)
    elif text in ["/today", "/today@talitrade_bot"]:
        await handle_today(chat_id)
    elif text in ["/journal", "/journal@talitrade_bot"]:
        await handle_journal(chat_id)
    elif text in ["/news", "/news@talitrade_bot"]:
        await handle_news(chat_id)
    elif text in ["/help", "/help@talitrade_bot"]:
        await send_telegram(
            "🤖 <b>TaliTrade Commands</b>\n\n"
            "/status — Live risk snapshot\n"
            "/today — Today's trades & P&L\n"
            "/journal — Last 10 trades\n"
            "/news — Upcoming high-impact news\n"
            "/help — This message",
            chat_id=chat_id
        )
    return {"ok": True}


async def handle_news(chat_id: str):
    events  = await fetch_news_calendar()
    now     = datetime.now(timezone.utc)
    upcoming = []
    for event in events:
        if not is_index_relevant(event):
            continue
        event_time = parse_event_time(event)
        if not event_time:
            continue
        mins = (event_time - now).total_seconds() / 60
        if 0 < mins < 480:
            upcoming.append((mins, event, event_time))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        await send_telegram("📅 No high-impact news in the next 8 hours.", chat_id=chat_id)
        return
    lines = []
    for mins, event, event_time in upcoming[:8]:
        et_offset = -4 if 3 <= event_time.month <= 11 else -5
        et_time = event_time + timedelta(hours=et_offset)
        time_str = et_time.strftime("%I:%M %p")
        when = f"in {round(mins)}m" if mins < 60 else f"in {round(mins/60,1)}h"
        lines.append(f"🔴 <b>{event.get('title','?')}</b> — {time_str} ET ({when})")
    await send_telegram(
        f"📅 <b>Upcoming High-Impact News</b>\n<i>DJI30, NAS100, SP500</i>\n{'─'*28}\n\n"
        + "\n".join(lines)
        + f"\n\n{'─'*28}\n⚠️ No trades within 5 min before/after each event.",
        chat_id=chat_id
    )


async def handle_status(chat_id: str):
    if not account_data_store:
        await send_telegram("📡 No data — open FundingPips in your browser first.", chat_id=chat_id)
        return
    acct_id = "1917136" if "1917136" in account_data_store else list(account_data_store.keys())[0]
    acct    = account_data_store[acct_id]
    balance = acct.get("balance") or 0
    equity  = acct.get("equity")  or 0
    profit  = acct.get("profit")  or 0
    risk    = acct.get("riskPerTradeIdea") or {}
    daily   = acct.get("dailyLoss")        or {}
    overall = acct.get("overallLoss")      or {}
    acct_type = acct.get("accountType", "unknown")
    acct_size = acct.get("accountSize", 10000)
    last = (acct.get("last_updated") or "")[:19].replace("T", " ")

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
    await send_telegram(
        f"📊 <b>TaliTrade — {acct_id}</b>\n<i>{acct_type} | ${acct_size/1000:.0f}K</i>\n{'─'*28}\n\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n"
        f"📈 Equity:  <b>${equity:.2f}</b>\n"
        f"📉 P&L:     <b>{'+'if profit>=0 else ''}{profit:.2f}</b>\n\n"
        f"{'─'*28}\n{risk_line}"
        f"{icon(daily.get('pct'))} <b>Daily Loss</b>  {daily.get('pct',0)}%\n"
        f"  {bar(daily.get('pct',0))}  ${daily.get('used',0):.0f} / ${daily.get('limit',500):.0f}\n"
        f"  Remaining: <b>${daily.get('remaining',500):.0f}</b>\n\n"
        f"{icon(overall.get('pct'))} <b>Overall Loss</b>  {overall.get('pct',0)}%\n"
        f"  {bar(overall.get('pct',0))}  ${overall.get('used',0):.0f} / ${overall.get('limit',1000):.0f}\n"
        f"  Remaining: <b>${overall.get('remaining',1000):.0f}</b>\n\n"
        f"{'─'*28}\n🕐 {last} UTC",
        chat_id=chat_id
    )


async def handle_today(chat_id: str):
    today_trades_raw = await db_get_trades_today(account_id="1917136")
    today_trades = [row_to_trade(r) for r in today_trades_raw]
    today = date.today().isoformat()
    if not today_trades:
        await send_telegram(f"📅 No trades logged today ({today}).", chat_id=chat_id)
        return
    total_pnl = sum(t.get("pnl") or 0 for t in today_trades)
    wins      = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    win_rate  = round(len(wins) / len(today_trades) * 100)
    lines = [
        f"{'✅' if (t.get('pnl') or 0) > 0 else '❌'} {t.get('symbol','?')} {t.get('direction','?')} "
        f"<b>{'+'if(t.get('pnl')or 0)>=0 else ''}{(t.get('pnl')or 0):.2f}</b> @ {(t.get('closedAt') or '')[11:16]}"
        for t in today_trades
    ]
    await send_telegram(
        f"📅 <b>Today — {today}</b>\n{'─'*28}\n\n"
        + "\n".join(lines)
        + f"\n\n{'─'*28}\n"
          f"P&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> | "
          f"{len(today_trades)} trades | WR: {win_rate}%",
        chat_id=chat_id
    )


async def handle_journal(chat_id: str):
    recent_raw = await db_get_trades(account_id="1917136", limit=10)
    recent = [row_to_trade(r) for r in recent_raw]
    if not recent:
        await send_telegram("📒 No trades in journal yet.", chat_id=chat_id)
        return
    total_pnl = sum(t.get("pnl") or 0 for t in recent)
    wins = len([t for t in recent if (t.get("pnl") or 0) > 0])
    lines = [
        f"{'✅' if (t.get('pnl') or 0) > 0 else '❌'} <b>{t.get('symbol','?')}</b> "
        f"{t.get('direction','?')} {'+'if(t.get('pnl')or 0)>=0 else ''}{(t.get('pnl')or 0):.2f} | "
        f"{(t.get('closedAt') or '')[:10]}"
        for t in recent
    ]
    await send_telegram(
        f"📒 <b>Last {len(recent)} Trades</b>\n{'─'*28}\n\n"
        + "\n".join(lines)
        + f"\n\n{'─'*28}\n"
          f"P&L: <b>{'+'if total_pnl>=0 else ''}{total_pnl:.2f}</b> | WR: {round(wins/len(recent)*100)}%",
        chat_id=chat_id
    )


# ── Extension endpoints ───────────────────────────────────────────────────────
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
    timestamp: str | None = None
    url: str | None = None


@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    account_id = data.accountId or "unknown"
    prev = account_data_store.get(account_id, {})
    account_data_store[account_id] = {**data.dict(), "last_updated": datetime.utcnow().isoformat()}
    for alert in data.alerts:
        await send_telegram(alert.get("message", "") + f"\n\n<i>Account: {account_id}</i>")
    prev_profit = prev.get("profit")
    curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        if prev_profit - curr_profit >= 10:
            await send_telegram(
                f"📉 <b>Profit Drop</b>\nAccount: {account_id}\n"
                f"${prev_profit:.2f} → ${curr_profit:.2f}  (-${prev_profit - curr_profit:.2f})"
            )
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(f"✅ <b>Position in Profit!</b>\nAccount: {account_id} | ${curr_profit:.2f}")
    risk    = data.riskPerTradeIdea or {}
    daily   = data.dailyLoss        or {}
    overall = data.overallLoss      or {}
    return {
        "status": "ok", "account": account_id,
        "balance": data.balance, "equity": data.equity,
        "tradeRisk":   {"used": risk.get("combined"),   "remaining": risk.get("remaining"),   "pct": risk.get("pct")},
        "dailyLoss":   {"used": daily.get("used"),       "remaining": daily.get("remaining"),   "pct": daily.get("pct")},
        "overallLoss": {"used": overall.get("used"),     "remaining": overall.get("remaining"), "pct": overall.get("pct")},
        "alerts_fired": len(data.alerts)
    }


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


@app.post("/extension/trade")
async def log_trade(trade: TradeData):
    # Persist to Neon
    await db_insert_trade(trade.dict())

    pnl         = trade.pnl or 0
    icon        = "✅" if pnl > 0 else "❌"
    daily_pct   = round((trade.dailyLossUsed   or 0) / (trade.dailyLossLimit   or 500)  * 100)
    overall_pct = round((trade.overallLossUsed or 0) / (trade.overallLossLimit or 1000) * 100)
    await send_telegram(
        f"{icon} <b>Trade Closed</b>\nAccount: {trade.accountId}\n"
        f"{trade.symbol} {trade.direction} | <b>{'+'if pnl>=0 else ''}{pnl:.2f}</b>\n"
        f"Balance: ${trade.balanceAfter:.2f}\n"
        f"Daily: {daily_pct}% | Overall: {overall_pct}%"
    )
    return {"status": "ok", "persisted": True}


@app.get("/extension/journal")
async def get_journal(account_id: str = None, limit: int = 50):
    rows   = await db_get_trades(account_id=account_id, limit=limit)
    trades = [row_to_trade(r) for r in rows]
    return {"trades": trades, "total": len(trades)}


@app.get("/extension/status")
async def extension_status():
    return {"accounts": account_data_store, "count": len(account_data_store)}


@app.get("/extension/news")
async def get_news():
    events  = await fetch_news_calendar()
    now     = datetime.now(timezone.utc)
    upcoming = []
    for event in events:
        if not is_index_relevant(event):
            continue
        event_time = parse_event_time(event)
        if not event_time:
            continue
        minutes_until = (event_time - now).total_seconds() / 60
        if -60 < minutes_until < 480:
            et_offset = -4 if 3 <= event_time.month <= 11 else -5
            et_time = event_time + timedelta(hours=et_offset)
            upcoming.append({
                "title":         event.get("title"),
                "currency":      event.get("country"),
                "time_et":       et_time.strftime("%I:%M %p ET"),
                "time_utc":      event_time.isoformat(),
                "minutes_until": round(minutes_until),
                "forecast":      event.get("forecast"),
                "previous":      event.get("previous"),
            })
    upcoming.sort(key=lambda x: x["minutes_until"])
    return {"events": upcoming[:10]}


# ── Test endpoints ────────────────────────────────────────────────────────────
@app.get("/test/telegram")
async def test_telegram():
    await send_telegram("🚀 <b>TaliTrade v3 live!</b>\nJournal now persisted to Neon ✅")
    return {"status": "sent"}


@app.get("/test/news")
async def test_news():
    await send_telegram(
        "🗞 <b>HIGH-IMPACT NEWS IN 10 MIN [TEST]</b>\n──────────────────────────────\n"
        "📌 <b>US Non-Farm Payrolls</b>\n🌍 Currency: <b>USD</b>\n"
        "🕐 Time: <b>08:30 AM ET</b>\n📊 Affects: <b>DJI30, NAS100, SP500</b>\n"
        "──────────────────────────────\n"
        "⚡ <b>NFP — biggest mover for indices.</b>\n"
        "⚠️ No trades within 5 min before/after this event."
    )
    return {"status": "sent"}


@app.get("/test/weekend")
async def test_weekend():
    await send_telegram(
        "⚠️ <b>MARKET CLOSES IN 1 HOUR [TEST]</b>\n──────────────────────────────\n"
        "🗓 Friday close: <b>5:00 PM ET</b>\n"
        "📊 Affects: DJI30, NAS100, SP500, Forex, Gold\n"
        "──────────────────────────────\n"
        "⚠️ Holding over weekend is <b>not permitted</b>.\n"
        "Profits will <b>not count</b> — close before 5 PM ET."
    )
    return {"status": "sent"}


@app.get("/test/db")
async def test_db():
    """Confirm trades table exists and show row count."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM trades"))
        count = result.scalar()
    return {"status": "ok", "trades_in_db": count}
