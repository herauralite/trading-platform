import os
import logging
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import datetime

if int(os.getenv("WEB_CONCURRENCY", "1")) > 1:
    raise RuntimeError("Multi-worker deployment requires Redis-backed SSE. Set WEB_CONCURRENCY=1.")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting trading platform...")
    # Register Telegram webhook on startup
    await setup_telegram_webhook()
    yield
    logger.info("Shutting down...")


app = FastAPI(title="Trading Platform", version="1.0.0", lifespan=lifespan)

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
RAILWAY_URL = "https://trading-platform-production-70e0.up.railway.app"

async def send_telegram(message: str, chat_id: str = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not cid:
        print(f"Telegram not configured. Message: {message}")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": message, "parse_mode": "HTML"}
            )
    except Exception as e:
        print(f"Telegram error: {e}")


async def setup_telegram_webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    webhook_url = f"{RAILWAY_URL}/telegram/webhook"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": webhook_url}
            )
            logger.info(f"Telegram webhook set: {res.json()}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")


# ── Telegram Webhook (handles /status command) ────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    message = body.get("message", {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))

    if text.strip().lower() in ["/status", "/status@talitrade_bot"]:
        await handle_status_command(chat_id)
    elif text.strip().lower() in ["/help", "/help@talitrade_bot"]:
        await send_telegram(
            "🤖 <b>TaliTrade Bot Commands</b>\n\n"
            "/status — Live account risk snapshot\n"
            "/help — Show this message",
            chat_id=chat_id
        )

    return {"ok": True}


async def handle_status_command(chat_id: str):
    if not account_data_store:
        await send_telegram("📡 No data yet — open FundingPips in your browser first.", chat_id=chat_id)
        return

    # Get most recent account (funded account 1917136 preferred)
    acct_id = "1917136" if "1917136" in account_data_store else list(account_data_store.keys())[0]
    acct = account_data_store[acct_id]

    balance = acct.get("balance")
    equity = acct.get("equity")
    profit = acct.get("profit")
    risk = acct.get("riskPerTradeIdea") or {}
    daily = acct.get("dailyLoss") or {}
    overall = acct.get("overallLoss") or {}
    last = acct.get("last_updated", "")[:19].replace("T", " ")

    def bar(pct):
        filled = round((pct or 0) / 10)
        empty = 10 - filled
        return "█" * filled + "░" * empty

    def level_icon(pct):
        if pct is None: return "⚪"
        if pct >= 90: return "🚨"
        if pct >= 75: return "🔴"
        if pct >= 50: return "⚠️"
        return "✅"

    msg = (
        f"📊 <b>TaliTrade Status — {acct_id}</b>\n"
        f"{'─' * 28}\n\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n"
        f"📈 Equity:  <b>${equity:.2f}</b>\n"
        f"📉 P&L:     <b>{'+'if profit>=0 else ''}{profit:.2f}</b>\n\n"
        f"{'─' * 28}\n"
        f"{level_icon(risk.get('pct'))} <b>Trade Idea Risk</b>  {risk.get('pct',0)}%\n"
        f"  {bar(risk.get('pct',0))}  ${risk.get('combined',0):.0f} / $300\n"
        f"  Remaining: <b>${risk.get('remaining',300):.0f}</b>\n\n"
        f"{level_icon(daily.get('pct'))} <b>Daily Loss</b>       {daily.get('pct',0)}%\n"
        f"  {bar(daily.get('pct',0))}  ${daily.get('used',0):.0f} / $500\n"
        f"  Remaining: <b>${daily.get('remaining',500):.0f}</b>\n\n"
        f"{level_icon(overall.get('pct'))} <b>Overall Loss</b>     {overall.get('pct',0)}%\n"
        f"  {bar(overall.get('pct',0))}  ${overall.get('used',0):.0f} / $1000\n"
        f"  Remaining: <b>${overall.get('remaining',1000):.0f}</b>\n\n"
        f"{'─' * 28}\n"
        f"🕐 {last} UTC"
    )

    await send_telegram(msg, chat_id=chat_id)


# ── Extension Data Store ──────────────────────────────────────────────────────
account_data_store = {}


class ExtensionData(BaseModel):
    profit: float | None = None
    balance: float | None = None
    equity: float | None = None
    accountId: str | None = None
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

    # Fire rule engine alerts
    for alert in data.alerts:
        msg = alert.get("message", "")
        await send_telegram(msg + f"\n\n<i>Account: {account_id}</i>")

    # Profit drop alert
    prev_profit = prev.get("profit")
    curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        if prev_profit - curr_profit >= 10:
            await send_telegram(
                f"📉 <b>Profit Drop</b>\n"
                f"Account: {account_id}\n"
                f"${prev_profit:.2f} → ${curr_profit:.2f}  (-${prev_profit - curr_profit:.2f})"
            )
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(
                f"✅ <b>Position in Profit!</b>\n"
                f"Account: {account_id} | ${curr_profit:.2f}"
            )

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


@app.get("/extension/status")
async def extension_status():
    return {"accounts": account_data_store, "count": len(account_data_store)}


@app.get("/test/telegram")
async def test_telegram():
    await send_telegram("🚀 <b>TaliTrade is live!</b>\nType /status for a real-time snapshot.")
    return {"status": "sent"}
