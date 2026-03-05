import os
import logging
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

# Load .env first before anything else
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

if int(os.getenv("WEB_CONCURRENCY", "1")) > 1:
    raise RuntimeError("Multi-worker deployment requires Redis-backed SSE. Set WEB_CONCURRENCY=1.")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting trading platform...")
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="Trading Platform",
    version="1.0.0",
    lifespan=lifespan,
)

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
        result = await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"Telegram not configured. Message: {message}")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            )
    except Exception as e:
        print(f"Telegram error: {e}")


# ── Extension Data Store ──────────────────────────────────────────────────────
account_data_store = {}


# ── Pydantic Model ────────────────────────────────────────────────────────────
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


# ── Main Extension Endpoint ───────────────────────────────────────────────────
@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    account_id = data.accountId or "unknown"
    prev = account_data_store.get(account_id, {})

    # Store latest snapshot
    account_data_store[account_id] = {
        **data.dict(),
        "last_updated": datetime.utcnow().isoformat()
    }

    # Fire Telegram for rule engine alerts (risk per trade, daily loss, overall loss)
    for alert in data.alerts:
        msg = alert.get("message", "")
        await send_telegram(msg + f"\n\n<i>Account: {account_id}</i>")

    # Profit drop alert
    prev_profit = prev.get("profit")
    curr_profit = data.profit
    if curr_profit is not None and prev_profit is not None:
        drop = prev_profit - curr_profit
        if drop >= 10:
            await send_telegram(
                f"📉 <b>Profit Drop Alert</b>\n"
                f"Account: {account_id}\n"
                f"${prev_profit:.2f} → ${curr_profit:.2f}\n"
                f"Drop: -${drop:.2f}"
            )
        if prev_profit < 0 and curr_profit >= 0:
            await send_telegram(
                f"✅ <b>Position in Profit!</b>\n"
                f"Account: {account_id} | Profit: ${curr_profit:.2f}"
            )

    risk = data.riskPerTradeIdea or {}
    daily = data.dailyLoss or {}
    overall = data.overallLoss or {}

    return {
        "status": "ok",
        "account": account_id,
        "balance": data.balance,
        "equity": data.equity,
        "tradeRisk": {
            "used": risk.get("combined"),
            "remaining": risk.get("remaining"),
            "pct": risk.get("pct"),
            "limit": risk.get("limit")
        },
        "dailyLoss": {
            "used": daily.get("used"),
            "remaining": daily.get("remaining"),
            "pct": daily.get("pct")
        },
        "overallLoss": {
            "used": overall.get("used"),
            "remaining": overall.get("remaining"),
            "pct": overall.get("pct")
        },
        "alerts_fired": len(data.alerts)
    }


@app.get("/extension/status")
async def extension_status():
    return {
        "accounts": account_data_store,
        "count": len(account_data_store)
    }


@app.get("/test/telegram")
async def test_telegram():
    await send_telegram("🚀 <b>TaliTrade bot is connected!</b>\nRule engine alerts are working.")
    return {"status": "sent"}
