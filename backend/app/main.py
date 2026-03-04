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

from app.services.matchtrade import MatchTraderClient

@app.get("/test/matchtrade")
async def test_matchtrade():
    client = MatchTraderClient(
        server_url="https://mtr-platform.fundingpips.com",
        email="btctrey@icloud.com",
        password="f2594b45ef"
    )
    success = await client.authenticate()
    if not success:
        return {"status": "failed", "message": "Authentication failed"}
    
    account = await client.get_account()
    positions = await client.get_positions()
    
    return {
        "status": "connected",
        "account": account,
        "positions": len(positions)
    }

@app.get("/test/matchtrade/debug")
async def test_matchtrade_debug():
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://mtr-platform.fundingpips.com/mtr-core-edge/login",
            json={
                "email": "btctrey@icloud.com",
                "password": "YOUR_NEW_PASSWORD",
                "partnerId": "1",
            }
        )
        return {
            "status_code": resp.status_code,
            "response": resp.text
        }

@app.get("/test/account")
async def test_account():
    import httpx
    trading_api_token = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJNdHIgVHJhZGluZyBBcGkiLCJ0cmFkaW5nQWNjb3VudElkIjoiMTg1NTA0OSIsImVtYWlsIjoiYnRjdHJleUBpY2xvdWQuY29tIiwicGFydG5lcklkIjoiMSIsImlhdCI6MTc3MjE2MzUyNiwiZXhwIjo0NjIyNDcwNDIyfQ.eJMGN366ehCpKb3XB97DjvXl9HztwauLEX9Bl8pbFnM"
    trading_account_token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4MDM2OTk1MjYsInRyYWRpbmdfYWNjb3VudF91dWlkIjoiMjY0OTIzODEtYzkwYy00Y2UwLTg5YjAtMTUzMTZmNDFhNWYxIiwiYWNjb3VudF91dWlkIjoiNGY0YzExNDMtZjQwOC00ODE3LWIxMmMtNWE3NjI5ZjU2NTk0Iiwic3lzdGVtX3V1aWQiOiJiZWVkYmVhOS1jNzU3LTQ2YWQtYjkzYi1hNTJiYTJjM2Q2NDgiLCJsb2dpbiI6IjE4NTUwNDkifQ.GIsrXXfSaF-DbEr-d_Q6Q1mA_ZfrQb518OTgy1FYEtg"
    
    headers = {
        "Authorization": f"Bearer {trading_api_token}",
        "trading-account-token": trading_account_token,
        "Content-Type": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://mtr-platform.fundingpips.com/mtr-core-edge/account",
            headers=headers
        )
        return {
            "status_code": resp.status_code,
            "response": resp.json() if resp.status_code == 200 else resp.text
        }

from pydantic import BaseModel
from typing import Optional
from datetime import datetime

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
    account_data_store[data.accountId or "unknown"] = {
        "profit": data.profit,
        "has_positions": data.hasPositions,
        "positions": data.positions,
        "timestamp": data.timestamp,
        "last_seen": datetime.utcnow().isoformat()
    }
    return {"status": "ok"}

@app.get("/extension/status")
async def extension_status():
    return account_data_store
