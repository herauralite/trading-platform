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
from app.core import ingest_auth
from app.core.ingest_auth import Principal

router = APIRouter(prefix="/ingest", tags=["connector-ingest"])


async def _owned_payload(payload, principal: Principal) -> dict:
    """Bind an ingest payload to the authenticated user and verify account ownership.

    The owner always comes from the session. A ``user_id`` in the body is refused, and the
    target account must be unowned (claim) or already owned by the caller.
    """
    if str(getattr(payload, "user_id", None) or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Explicit user_id is not accepted on authenticated ingest routes",
        )
    normalized = payload.model_dump()
    normalized["user_id"] = principal.user_id
    if normalized.get("external_account_id") is not None:
        normalized["external_account_id"] = await ingest_auth.authorize_account_write(
            principal.user_id, normalized.get("external_account_id")
        )
    return normalized


@router.post("/accounts")
async def ingest_accounts(
    payload: IngestTradingAccount,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    account = await upsert_trading_account(await _owned_payload(payload, principal))
    return {"ok": True, "account": account}


@router.post("/account-snapshots")
async def ingest_account_snapshots(
    payload: IngestAccountSnapshot,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    await ingest_account_snapshot(await _owned_payload(payload, principal))
    return {"ok": True}


@router.post("/positions")
async def ingest_positions(
    payload: IngestPosition,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    await ingest_position(await _owned_payload(payload, principal))
    return {"ok": True}


@router.post("/trades")
async def ingest_trades(
    payload: IngestTrade,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    inserted = await ingest_trade(await _owned_payload(payload, principal))
    if not inserted:
        raise HTTPException(status_code=422, detail="Trade rejected: pnl is missing or exceeds account_size")
    return {"ok": True, "persisted": inserted}


@router.post("/events")
async def ingest_events(
    payload: IngestEvent,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    await ingest_event(await _owned_payload(payload, principal))
    return {"ok": True}


@router.post("/csv/trades")
async def ingest_csv_trades(
    payload: CsvTradeImportRequest,
    principal: Principal = Depends(ingest_auth.require_session_principal),
):
    owned = await _owned_payload(payload, principal)
    resolved_user_id = principal.user_id
    external_account_id = owned["external_account_id"]
    # CSV import connector path: creates account (if needed) and imports normalized trades.
    account_payload = {
        "user_id": resolved_user_id,
        "connector_type": payload.connector_type,
        "broker_name": payload.broker_name,
        "external_account_id": external_account_id,
        "account_type": payload.account_type,
        "account_size": payload.account_size,
        "display_label": f"CSV {external_account_id}",
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
        row_payload["external_account_id"] = external_account_id
        row_payload["user_id"] = resolved_user_id
        if payload.account_type and not row_payload.get("account_type"):
            row_payload["account_type"] = payload.account_type
        if payload.account_size and not row_payload.get("account_size"):
            row_payload["account_size"] = payload.account_size
        if await ingest_trade(row_payload):
            persisted += 1

    return {"ok": True, "connector": payload.connector_type, "imported": persisted, "received": len(payload.rows)}
