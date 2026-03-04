import os
import logging
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


# Extension data store
account_data_store = {}


class ExtensionData(BaseModel):
    profit: float
    accountId: Optional[str] = None
    hasPositions: bool
    positions: list = []
    timestamp: str
    url: str


@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    from app.services.telegram_bot import send_alert

    prev = account_data_store.get(data.accountId or "unknown", {})
    prev_profit = prev.get("profit", 0)

    account_data_store[data.accountId or "unknown"] = {
        "profit": data.profit,
        "has_positions": data.hasPositions,
        "positions": data.positions,
        "timestamp": data.timestamp,
        "last_seen": datetime.utcnow().isoformat()
    }

    # Alert if profit dropped by more than $10
    if prev_profit - data.profit >= 10:
        await send_alert(
            f"⚠️ <b>Profit Drop Alert</b>\n"
            f"Account: {data.accountId}\n"
            f"Profit: ${data.profit:.2f} (was ${prev_profit:.2f})\n"
            f"Change: -${prev_profit - data.profit:.2f}"
        )

    # Alert if profit just went positive
    if data.profit > 0 and prev_profit == 0:
        await send_alert(
            f"✅ <b>Position in Profit</b>\n"
            f"Account: {data.accountId}\n"
            f"Profit: ${data.profit:.2f}"
        )

    return {"status": "ok"}


@app.get("/extension/status")
async def extension_status():
    return account_data_store


@app.get("/test/telegram")
async def test_telegram():
    from app.services.telegram_bot import send_alert
    await send_alert("🚀 <b>TaliTrade bot is connected!</b>\nAlerts are working.")
    return {"status": "sent"}
