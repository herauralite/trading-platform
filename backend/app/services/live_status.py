"""Durable "latest heartbeat" per trading account.

The dashboard's live view (balance, equity, positions, risk) used to exist only in the web process's
memory (main.account_data_store): a restart or a second replica showed nothing until the extension's
next heartbeat, and stale accounts simply vanished. Each heartbeat is now also written to
trading_accounts.metadata.last_heartbeat (no schema change), stamped with the SERVER receipt time, so any
process can rebuild the view and say exactly how old it is.

Only display fields are stored; the page URL, Telegram id and queued close notifications are not.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

HEARTBEAT_FIELDS = ("profit", "balance", "equity", "accountType", "accountSize", "accountLabel", "isMaster",
                    "hasPositions", "openPositionCount", "positions", "riskPerTradeIdea", "dailyLoss",
                    "overallLoss", "alerts", "timestamp")


def _engine():
    from app.core.database import engine
    return engine


def sanitize(data: dict[str, Any]) -> dict[str, Any]:
    return {k: data.get(k) for k in HEARTBEAT_FIELDS}


async def persist_heartbeat(*, trading_account_id: int, user_id: str, data: dict[str, Any], received_at: datetime) -> None:
    hb = {**sanitize(data), "received_at": received_at.astimezone(timezone.utc).isoformat()}
    async with _engine().begin() as conn:
        await conn.execute(text("""
            UPDATE trading_accounts
            SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('last_heartbeat', CAST(:hb AS jsonb))
            WHERE id = :id AND user_id = :uid
        """), {"hb": json.dumps(hb, default=str), "id": trading_account_id, "uid": str(user_id)})


async def load_heartbeats(user_id: str, account_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{external_account_id: heartbeat} for the user's active accounts that have one."""
    sql = """
        SELECT external_account_id, metadata->'last_heartbeat' AS hb
        FROM trading_accounts
        WHERE user_id = :uid AND is_active = TRUE AND metadata ? 'last_heartbeat'
    """
    params: dict[str, Any] = {"uid": str(user_id)}
    if account_ids:
        sql += " AND external_account_id = ANY(CAST(:ids AS TEXT[]))"
        params["ids"] = list(account_ids)
    async with _engine().connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    out = {}
    for row in rows:
        hb = row["hb"]
        if isinstance(hb, str):
            hb = json.loads(hb)
        if isinstance(hb, dict) and hb.get("received_at"):
            out[str(row["external_account_id"])] = hb
    return out


def received_at_naive_utc(hb: dict[str, Any]) -> str | None:
    """The store keeps naive-UTC ISO strings (datetime.utcnow().isoformat()); convert to match."""
    try:
        dt = datetime.fromisoformat(str(hb["received_at"]).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()
