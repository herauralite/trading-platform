import os
import logging
import httpx
import json
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import datetime, date
from collections import defaultdict

if int(os.getenv("WEB_CONCURRENCY", "1")) > 1:
    raise RuntimeError("Multi-worker deployment requires Redis-backed SSE. Set WEB_CONCURRENCY=1.")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

RAILWAY_URL = "https://trading-platform-production-70e0.up.railway.app"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TaliTrade backend...")
    await setup_telegram_webhook()
    yield
    logger.info("Shutting down...")


app = FastAPI(title="TaliTrade", version="2.0.0", lifespan=lifespan)

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
from sqlalchemy import text

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
            logger.info(f"Webhook set: {res.json()}")
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")


# ── In-Memory Stores ──────────────────────────────────────────────────────────
account_data_store: dict = {}
trade_journal: list = []          # all trades ever logged this session
trade_journal_by_account: dict = defaultdict(list)


# ── Telegram Webhook ──────────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    message = body.get("message", {})
    text = message.get("text", "").strip().lower().split("@")[0]
    chat_id = str(message.get("chat", {}).get("id", ""))

    if text == "/status":
        await handle_status_command(chat_id)
    elif text == "/today":
        await handle_today_command(chat_id)
    elif text == "/journal":
        await handle_journal_command(chat_id)
    elif text == "/help":
        await send_telegram(
            "🤖 <b>TaliTrade Commands</b>\n\n"
            "/status — Live risk snapshot\n"
            "/today — Today's trade summary\n"
            "/journal — Last 10 trades\n"
            "/help — This message",
            chat_id=chat_id
        )

    return {"ok": True}


async def handle_status_command(chat_id: str):
    if not account_data_store:
        await send_telegram("📡 No data — open FundingPips in your browser first.", chat_id=chat_id)
        return

    acct_id = "1917136" if "1917136" in account_data_store else list(account_data_store.keys())[0]
    acct = account_data_store[acct_id]

    balance = acct.get("balance") or 0
    equity = acct.get("equity") or 0
    profit = acct.get("profit") or 0
    risk = acct.get("riskPerTradeIdea") or {}
    daily = acct.get("dailyLoss") or {}
    overall = acct.get("overallLoss") or {}
    acct_type = acct.get("accountType", "unknown")
    acct_size = acct.get("accountSize", 10000)
    last = acct.get("last_updated", "")[:19].replace("T", " ")

    def bar(pct):
        filled = round((pct or 0) / 10)
        return "█" * filled + "░" * (10 - filled)

    def icon(pct):
        if pct is None: return "⚪"
        if pct >= 90: return "🚨"
        if pct >= 75: return "🔴"
        if pct >= 50: return "⚠️"
        return "✅"

    risk_line = ""
    if risk.get("applicable", True) and risk.get("limit"):
        risk_line = (
            f"\n{icon(risk.get('pct'))} <b>Trade Idea Risk</b>  {risk.get('pct',0)}%\n"
            f"  {bar(risk.get('pct',0))}  ${risk.get('combined',0):.0f} / ${risk.get('limit',300):.0f}\n"
            f"  Remaining: <b>${risk.get('remaining',300):.0f}</b>\n"
        )

    msg = (
        f"📊 <b>TaliTrade — {acct_id}</b>\n"
        f"<i>{acct_type} • ${acct_size/1000:.0f}K</i>\n"
        f"{'─'*28}\n\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n"
        f"📈 Equity:  <b>${equity:.2f}</b>\n"
        f"📉 P&L:     <b>{'+'if profit>=0 else ''}{profit:.2f}</b>\n\n"
        f"{'─'*28}"
        f"{risk_line}"
        f"{icon(daily.get('pct'))} <b>Daily Loss</b>  {daily.get('pct',0)}%\n"
        f"  {bar(daily.get('pct',0))}  ${daily.get('used',0):.0f} / ${daily.get('limit',500):.0f}\n"
        f"  Remaining: <b>${daily.get('remaining',500):.0f}</b>\n\n"
        f"{icon(overall.get('pct'))} <b>Overall Loss</b>  {overall.get('pct',0)}%\n"
        f"  {bar(overall.get('pct',0))}  ${overall.get('used',0):.0f} / ${overall.get('limit',1000):.0f}\n"
        f"  Remaining: <b>${overall.get('remaining',1000):.0f}</b>\n\n"
        f"{'─'*28}\n"
        f"🕐 {last} UTC"
    )
    await send_telegram(msg, chat_id=chat_id)


async def handle_today_command(chat_id: str):
    today = date.today().isoformat()
    all_trades = trade_journal

    today_trades = [t for t in all_trades if t.get("closeTime", "").startswith(today)]

    if not today_trades:
        await send_telegram("📋 No trades closed today yet.", chat_id=chat_id)
        return

    wins = [t for t in today_trades if t.get("outcome") == "win"]
    losses = [t for t in today_trades if t.get("outcome") == "loss"]
    total_pnl = sum(t.get("pnl", 0) for t in today_trades)
    best = max(today_trades, key=lambda t: t.get("pnl", 0))
    worst = min(today_trades, key=lambda t: t.get("pnl", 0))

    msg = (
        f"📅 <b>Today's Summary</b>\n"
        f"{'─'*28}\n\n"
        f"Trades: <b>{len(today_trades)}</b>  "
        f"✅ {len(wins)} wins  ❌ {len(losses)} losses\n"
        f"Win rate: <b>{round(len(wins)/len(today_trades)*100)}%</b>\n\n"
        f"Total P&L: <b>{'+'if total_pnl>=0 else ''}${total_pnl:.2f}</b>\n"
        f"Best trade:  <b>+${best['pnl']:.2f}</b> ({best.get('symbol','?')} {best.get('direction','')})\n"
        f"Worst trade: <b>${worst['pnl']:.2f}</b> ({worst.get('symbol','?')} {worst.get('direction','')})\n\n"
    )

    # List each trade
    for t in today_trades[-10:]:  # last 10
        icon = "✅" if t.get("outcome") == "win" else "❌"
        pnl = t.get("pnl", 0)
        msg += f"{icon} {t.get('symbol','?')} {t.get('direction','')} ${pnl:+.2f}\n"

    await send_telegram(msg, chat_id=chat_id)


async def handle_journal_command(chat_id: str):
    if not trade_journal:
        await send_telegram("📋 No trades logged yet this session.", chat_id=chat_id)
        return

    recent = trade_journal[-10:][::-1]  # last 10, newest first
    msg = f"📓 <b>Last {len(recent)} Trades</b>\n{'─'*28}\n\n"

    for t in recent:
        icon = "✅" if t.get("outcome") == "win" else "❌"
        pnl = t.get("pnl", 0)
        close_time = t.get("closeTime", "")[:16].replace("T", " ")
        msg += (
            f"{icon} <b>{t.get('symbol','?')}</b> {t.get('direction','')} "
            f"${pnl:+.2f}  <i>{close_time}</i>\n"
        )

    wins = len([t for t in trade_journal if t.get("outcome") == "win"])
    total_pnl = sum(t.get("pnl", 0) for t in trade_journal)
    msg += (
        f"\n{'─'*28}\n"
        f"Session: {len(trade_journal)} trades | "
        f"{wins}W/{len(trade_journal)-wins}L | "
        f"P&L: ${total_pnl:+.2f}"
    )

    await send_telegram(msg, chat_id=chat_id)


# ── Extension Data Endpoint ───────────────────────────────────────────────────
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

    account_data_store[account_id] = {
        **data.dict(),
        "last_updated": datetime.utcnow().isoformat()
    }

    # Fire rule alerts
    for alert in data.alerts:
        await send_telegram(alert.get("message", "") + f"\n\n<i>Account: {account_id}</i>")

    # Profit drop alert
    prev_profit = prev.get("profit")
    curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        if prev_profit - curr_profit >= 10:
            await send_telegram(
                f"📉 <b>Profit Drop</b>\n"
                f"Account: {account_id}\n"
                f"${prev_profit:.2f} → ${curr_profit:.2f}  (-${prev_profit-curr_profit:.2f})"
            )
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(f"✅ <b>Position in Profit!</b>\nAccount: {account_id} | ${curr_profit:.2f}")

    risk = data.riskPerTradeIdea or {}
    daily = data.dailyLoss or {}
    overall = data.overallLoss or {}

    return {
        "status": "ok",
        "account": account_id,
        "balance": data.balance,
        "equity": data.equity,
        "tradeRisk": {"used": risk.get("combined"), "remaining": risk.get("remaining"), "pct": risk.get("pct")},
        "dailyLoss": {"used": daily.get("used"), "remaining": daily.get("remaining"), "pct": daily.get("pct")},
        "overallLoss": {"used": overall.get("used"), "remaining": overall.get("remaining"), "pct": overall.get("pct")},
        "alerts_fired": len(data.alerts)
    }


# ── Trade Journal Endpoint ────────────────────────────────────────────────────
class TradeRecord(BaseModel):
    accountId: str | None = None
    accountType: str | None = None
    accountSize: int | None = None
    symbol: str
    direction: str
    volume: float | None = None
    openPrice: float | None = None
    closePrice: float | None = None
    openTime: str | None = None
    closeTime: str | None = None
    pnl: float
    balanceAfter: float | None = None
    dailyLossUsed: float | None = None
    dailyLossLimit: float | None = None
    tradeIdeaLimit: float | None = None
    outcome: str  # 'win' or 'loss'


@app.post("/journal/trade")
async def log_trade(trade: TradeRecord):
    record = trade.dict()
    record["logged_at"] = datetime.utcnow().isoformat()

    trade_journal.append(record)
    account_id = trade.accountId or "unknown"
    trade_journal_by_account[account_id].append(record)

    # Send Telegram notification for closed trade
    icon = "✅" if trade.outcome == "win" else "❌"
    pnl_str = f"+${trade.pnl:.2f}" if trade.pnl >= 0 else f"-${abs(trade.pnl):.2f}"
    daily_info = ""
    if trade.dailyLossUsed is not None and trade.dailyLossLimit:
        daily_pct = round((trade.dailyLossUsed / trade.dailyLossLimit) * 100)
        daily_info = f"\nDaily loss used: {daily_pct}% (${trade.dailyLossUsed:.2f}/${trade.dailyLossLimit:.0f})"

    await send_telegram(
        f"{icon} <b>Trade Closed</b>\n"
        f"{trade.direction} {trade.symbol} — <b>{pnl_str}</b>\n"
        f"Account: {account_id}{daily_info}"
    )

    logger.info(f"Journal: {trade.outcome.upper()} {trade.direction} {trade.symbol} {pnl_str} [{account_id}]")
    return {"status": "logged", "total_trades": len(trade_journal)}


@app.get("/journal/trades")
async def get_trades(account_id: str = None, limit: int = 50):
    trades = trade_journal_by_account.get(account_id, trade_journal) if account_id else trade_journal
    return {
        "trades": trades[-limit:],
        "total": len(trades),
        "wins": len([t for t in trades if t.get("outcome") == "win"]),
        "losses": len([t for t in trades if t.get("outcome") == "loss"]),
        "total_pnl": round(sum(t.get("pnl", 0) for t in trades), 2)
    }


@app.get("/journal/today")
async def get_today_trades(account_id: str = None):
    today = date.today().isoformat()
    trades = trade_journal_by_account.get(account_id, trade_journal) if account_id else trade_journal
    today_trades = [t for t in trades if t.get("closeTime", "").startswith(today)]
    total_pnl = round(sum(t.get("pnl", 0) for t in today_trades), 2)
    wins = [t for t in today_trades if t.get("outcome") == "win"]
    return {
        "date": today,
        "trades": today_trades,
        "total": len(today_trades),
        "wins": len(wins),
        "losses": len(today_trades) - len(wins),
        "win_rate": round(len(wins) / len(today_trades) * 100) if today_trades else 0,
        "total_pnl": total_pnl
    }


@app.get("/extension/status")
async def extension_status():
    return {"accounts": account_data_store, "count": len(account_data_store)}


@app.get("/test/telegram")
async def test_telegram():
    await send_telegram("🚀 <b>TaliTrade v2 is live!</b>\nJournal + multi-account active.\n\nCommands: /status /today /journal")
    return {"status": "sent"}
