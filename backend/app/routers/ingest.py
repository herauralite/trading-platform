from fastapi import APIRouter, Depends, HTTPException

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
from app.core.auth_session import decode_session_token, get_bearer_token

router = APIRouter(prefix="/ingest", tags=["connector-ingest"])


def _resolve_authenticated_user_id(
    payload_user_id: str | None,
    token: str | None,
) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Missing authenticated session")
    session_user_id = str(decode_session_token(token)["sub"])
    explicit = str(payload_user_id or "").strip()
    if explicit:
        raise HTTPException(
            status_code=400,
            detail="Explicit user_id is not accepted on authenticated ingest routes",
        )
    return session_user_id


@router.post("/accounts")
async def ingest_accounts(
    payload: IngestTradingAccount,
    token: str | None = Depends(get_bearer_token),
):
    normalized = payload.model_dump()
    normalized["user_id"] = _resolve_authenticated_user_id(payload.user_id, token)
    account = await upsert_trading_account(normalized)
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
async def ingest_trades(
    payload: IngestTrade,
    token: str | None = Depends(get_bearer_token),
):
    normalized = payload.model_dump()
    normalized["user_id"] = _resolve_authenticated_user_id(payload.user_id, token)
    inserted = await ingest_trade(normalized)
    if not inserted:
        raise HTTPException(status_code=422, detail="Trade rejected: pnl exceeds account_size")
    return {"ok": True, "persisted": inserted}


@router.post("/events")
async def ingest_events(payload: IngestEvent):
    await ingest_event(payload.model_dump())
    return {"ok": True}


@router.post("/csv/trades")
async def ingest_csv_trades(
    payload: CsvTradeImportRequest,
    token: str | None = Depends(get_bearer_token),
):
    resolved_user_id = _resolve_authenticated_user_id(payload.user_id, token)
    # CSV import connector path: creates account (if needed) and imports normalized trades.
    account_payload = {
        "user_id": resolved_user_id,
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
            "import_provenance": {"connector": "csv_import"},
        }
        row_payload["connector_type"] = payload.connector_type
        row_payload["external_account_id"] = payload.external_account_id
        row_payload["user_id"] = resolved_user_id
        if payload.account_type and not row_payload.get("account_type"):
            row_payload["account_type"] = payload.account_type
        if payload.account_size and not row_payload.get("account_size"):
            row_payload["account_size"] = payload.account_size
        if await ingest_trade(row_payload):
            persisted += 1

    return {"ok": True, "connector": payload.connector_type, "imported": persisted, "received": len(payload.rows)}
