import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# Load .env first before anything else
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

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
    version="1.1.0",
    lifespan=lifespan,
)

# IMPORTANT:
# - avoid allow_credentials=True with wildcard origins for browser-posted extension traffic
# - explicitly allow the FundingPips origin that is posting from the content script
cors_origins = [
    "https://mtr-platform.fundingpips.com",
    "https://app.fundingpips.com",
]
extra_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
for origin in extra_origins:
    if origin not in cors_origins:
        cors_origins.append(origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import auth, accounts
from app.core.database import engine

app.include_router(auth.router)
app.include_router(accounts.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "trading-platform",
        "cors_origins": cors_origins,
    }


@app.get("/health/db")
async def health_db():
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected", "result": result.scalar()}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Extension in-memory account snapshot store
account_data_store: dict[str, dict] = {}


class ExtensionData(BaseModel):
    profit: Optional[float] = None
    balance: Optional[float] = None
    equity: Optional[float] = None
    accountId: Optional[str] = None
    accountType: Optional[str] = None
    accountSize: Optional[int] = None
    accountLabel: Optional[str] = None
    isMaster: bool = False
    hasPositions: bool
    openPositionCount: int = 0
    positions: list = []
    riskPerTradeIdea: Optional[dict] = None
    dailyLoss: Optional[dict] = None
    overallLoss: Optional[dict] = None
    alerts: list = []
    closedTrades: list = []
    timestamp: Optional[str] = None
    url: Optional[str] = None


class TradeData(BaseModel):
    accountId: Optional[str] = None
    accountType: Optional[str] = None
    accountSize: Optional[int] = None
    symbol: Optional[str] = None
    direction: Optional[str] = None
    volume: Optional[float] = None
    openPrice: Optional[float] = None
    closePrice: Optional[float] = None
    pnl: Optional[float] = None
    balanceAfter: Optional[float] = None
    equityAfter: Optional[float] = None
    dailyLossUsed: Optional[float] = None
    dailyLossLimit: Optional[float] = None
    overallLossUsed: Optional[float] = None
    overallLossLimit: Optional[float] = None
    closedAt: Optional[str] = None
    source: Optional[str] = "realtime"  # scraper | realtime


async def send_alert(message: str):
    from app.services.telegram_bot import send_alert as telegram_send_alert
    await telegram_send_alert(message)


async def db_insert_trade(trade_dict: dict):
    pnl_val = trade_dict.get("pnl") or 0
    account_size = trade_dict.get("accountSize") or 10000

    # hard reject if the scraper accidentally captured close price as P&L
    if abs(pnl_val) > account_size:
        logger.warning(
            "Rejected suspicious trade pnl=%s for %s on account %s because it exceeds account size %s",
            pnl_val,
            trade_dict.get("symbol"),
            trade_dict.get("accountId"),
            account_size,
        )
        return False

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO trades (
                    account_id, account_type, account_size,
                    symbol, direction, volume, open_price, close_price, pnl,
                    balance_after, equity_after,
                    daily_loss_used, daily_loss_limit,
                    overall_loss_used, overall_loss_limit,
                    closed_at, source
                ) VALUES (
                    :accountId, :accountType, :accountSize,
                    :symbol, :direction, :volume, :openPrice, :closePrice, :pnl,
                    :balanceAfter, :equityAfter,
                    :dailyLossUsed, :dailyLossLimit,
                    :overallLossUsed, :overallLossLimit,
                    :closedAt, :source
                )
                ON CONFLICT ON CONSTRAINT trades_dedup DO NOTHING
                """
            ),
            {
                k: trade_dict.get(k)
                for k in [
                    "accountId", "accountType", "accountSize", "symbol", "direction", "volume",
                    "openPrice", "closePrice", "pnl", "balanceAfter", "equityAfter",
                    "dailyLossUsed", "dailyLossLimit", "overallLossUsed", "overallLossLimit",
                    "closedAt", "source",
                ]
            },
        )
    return True


async def db_get_trades(account_id: Optional[str] = None, limit: int = 50, offset: int = 0, order: str = "desc", source: Optional[str] = "scraper") -> list[dict]:
    order_sql = "ASC" if order.lower() == "asc" else "DESC"
    source_clause = "" if not source or source == "all" else " AND (source = :src OR (source IS NULL AND :src = 'scraper'))"
    params: dict = {"l": limit, "o": offset}
    if source and source != "all":
        params["src"] = source

    async with engine.connect() as conn:
        if account_id:
            params["a"] = account_id
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE account_id = :a{source_clause} ORDER BY COALESCE(closed_at, logged_at) {order_sql} LIMIT :l OFFSET :o"),
                params,
            )
        else:
            result = await conn.execute(
                text(f"SELECT * FROM trades WHERE 1=1{source_clause} ORDER BY COALESCE(closed_at, logged_at) {order_sql} LIMIT :l OFFSET :o"),
                params,
            )
        return [dict(row) for row in result.mappings().all()]


async def db_get_trade_stats(account_id: Optional[str] = None) -> dict:
    async with engine.connect() as conn:
        if account_id:
            result = await conn.execute(
                text("SELECT COUNT(*) AS total, MIN(logged_at) AS oldest FROM trades WHERE account_id = :a"),
                {"a": account_id},
            )
        else:
            result = await conn.execute(text("SELECT COUNT(*) AS total, MIN(logged_at) AS oldest FROM trades"))
        row = result.mappings().one_or_none()
        if not row:
            return {"total": 0, "oldest_trade_date": None}
        oldest = row["oldest"]
        if oldest and hasattr(oldest, "isoformat"):
            oldest = oldest.isoformat()
        return {"total": row["total"] or 0, "oldest_trade_date": oldest}


def row_to_trade(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "accountId": row.get("account_id"),
        "accountType": row.get("account_type"),
        "accountSize": row.get("account_size"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "volume": row.get("volume"),
        "openPrice": row.get("open_price"),
        "closePrice": row.get("close_price"),
        "pnl": row.get("pnl"),
        "balanceAfter": row.get("balance_after"),
        "equityAfter": row.get("equity_after"),
        "dailyLossUsed": row.get("daily_loss_used"),
        "dailyLossLimit": row.get("daily_loss_limit"),
        "overallLossUsed": row.get("overall_loss_used"),
        "overallLossLimit": row.get("overall_loss_limit"),
        "closedAt": row.get("closed_at"),
        "loggedAt": row.get("logged_at"),
        "source": row.get("source") or "scraper",
    }


@app.post("/extension/data")
async def receive_extension_data(data: ExtensionData):
    account_id = data.accountId or "unknown"
    prev = account_data_store.get(account_id, {})
    prev_profit = prev.get("profit")

    account_data_store[account_id] = {
        **data.dict(),
        "last_updated": datetime.utcnow().isoformat(),
    }

    # fire only newly-raised rule alerts
    prev_alerts = {a.get("type"): a.get("level") for a in (prev.get("alerts") or [])}
    for alert in data.alerts:
        if prev_alerts.get(alert.get("type")) != alert.get("level"):
            await send_alert(alert.get("message", "") + f"\n\n<i>Account: {account_id}</i>")

    # fast Telegram close notifications only; scraper persists the trade rows
    for closed_trade in data.closedTrades:
        pnl = closed_trade.get("pnl") or 0
        icon = "✅" if pnl >= 0 else "❌"
        await send_alert(
            f"{icon} <b>Trade Closed</b>\n"
            f"Account: {account_id}\n"
            f"{closed_trade.get('symbol', '?')} {closed_trade.get('direction', '?')} | <b>{'+' if pnl >= 0 else ''}{pnl:.2f}</b>"
        )

    if prev_profit is not None and data.profit is not None:
        if prev_profit - data.profit >= 10:
            await send_alert(
                f"⚠️ <b>Profit Drop Alert</b>\n"
                f"Account: {account_id}\n"
                f"Profit: ${data.profit:.2f} (was ${prev_profit:.2f})\n"
                f"Change: -${prev_profit - data.profit:.2f}"
            )
        if prev_profit < 0 <= data.profit:
            await send_alert(
                f"✅ <b>Position in Profit</b>\n"
                f"Account: {account_id}\n"
                f"Profit: ${data.profit:.2f}"
            )

    return {"status": "ok", "account": account_id}


@app.post("/extension/trade")
async def log_trade(trade: TradeData):
    persisted = await db_insert_trade(trade.dict())
    return {"status": "ok", "persisted": persisted}


# backward-compatible alias in case an older extension build still uses this path
@app.post("/journal/trade")
async def log_trade_alias(trade: TradeData):
    return await log_trade(trade)


@app.get("/extension/journal")
async def get_journal(account_id: Optional[str] = None, limit: int = 50, offset: int = 0, order: str = "desc", source: Optional[str] = "scraper"):
    rows = await db_get_trades(account_id=account_id, limit=limit, offset=offset, order=order, source=source)
    return {
        "trades": [row_to_trade(row) for row in rows],
        "total": len(rows),
        "offset": offset,
        "limit": limit,
    }


@app.get("/extension/journal/stats")
async def get_journal_stats(account_id: Optional[str] = None):
    return await db_get_trade_stats(account_id=account_id)


@app.get("/extension/status")
async def extension_status():
    return {"accounts": account_data_store, "count": len(account_data_store)}


@app.get("/test/telegram")
async def test_telegram():
    await send_alert("🚀 <b>TaliTrade bot is connected!</b>\nAlerts are working.")
    return {"status": "sent"}
