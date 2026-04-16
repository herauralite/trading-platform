from fastapi import APIRouter, HTTPException

from app.schemas_ingest import (
    CsvTradeImportRequest,
    IngestAccountSnapshot,
    IngestEvent,
    IngestPosition,
    IngestTrade,
    IngestTradingAccount,
)
from app.services.connector_ingest import (
    ingest_account_snapshot,
    ingest_event,
    ingest_position,
    ingest_trade,
    upsert_trading_account,
)

router = APIRouter(prefix="/ingest", tags=["connector-ingest"])


@router.post("/accounts")
async def ingest_accounts(payload: IngestTradingAccount):
    account = await upsert_trading_account(payload.model_dump())
    return {"ok": True, "account": account}


@router.post("/account-snapshots")
async def ingest_account_snapshots(payload: IngestAccountSnapshot):
    await ingest_account_snapshot(payload.model_dump())
    return {"ok": True}


@router.post("/positions")
async def ingest_positions(payload: IngestPosition):
    await ingest_position(payload.model_dump())
    return {"ok": True}


@router.post("/trades")
async def ingest_trades(payload: IngestTrade):
    inserted = await ingest_trade(payload.model_dump())
    if not inserted:
        raise HTTPException(status_code=422, detail="Trade rejected: pnl exceeds account_size")
    return {"ok": True, "persisted": inserted}


@router.post("/events")
async def ingest_events(payload: IngestEvent):
    await ingest_event(payload.model_dump())
    return {"ok": True}


@router.post("/csv/trades")
async def ingest_csv_trades(payload: CsvTradeImportRequest):
    # CSV import connector path: creates account (if needed) and imports normalized trades.
    account_payload = {
        "user_id": payload.user_id,
        "connector_type": payload.connector_type,
        "broker_name": payload.broker_name,
        "external_account_id": payload.external_account_id,
        "account_type": payload.account_type,
        "account_size": payload.account_size,
        "display_label": f"CSV {payload.external_account_id}",
    }
    await upsert_trading_account(account_payload)

    persisted = 0
    for row in payload.rows:
        row_payload = {
            "symbol": row.get("symbol"),
            "side": row.get("side") or row.get("direction"),
            "size": row.get("size") or row.get("volume"),
            "entry_price": row.get("entry_price") or row.get("open_price"),
            "exit_price": row.get("exit_price") or row.get("close_price"),
            "open_time": row.get("open_time"),
            "close_time": row.get("close_time") or row.get("closed_at"),
            "pnl": row.get("pnl"),
            "fees": row.get("fees"),
            "tags": row.get("tags") or [],
            "source_metadata": {"raw": row, "import": "csv"},
        }
        row_payload["connector_type"] = payload.connector_type
        row_payload["external_account_id"] = payload.external_account_id
        row_payload["user_id"] = payload.user_id
        if payload.account_type and not row_payload.get("account_type"):
            row_payload["account_type"] = payload.account_type
        if payload.account_size and not row_payload.get("account_size"):
            row_payload["account_size"] = payload.account_size
        if await ingest_trade(row_payload):
            persisted += 1

    return {"ok": True, "connector": payload.connector_type, "imported": persisted, "received": len(payload.rows)}
