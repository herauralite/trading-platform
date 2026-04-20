from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.database import engine
from app.services.connector_ingest import (
    get_connector_lifecycle,
    upsert_connector_lifecycle,
    upsert_trading_account,
)

logger = logging.getLogger(__name__)

FUNDINGPIPS_CONNECTOR_TYPE = "fundingpips_extension"


async def hydrate_fundingpips_canonical_state(
    telegram_user_id: str,
    *,
    trigger: str,
) -> dict[str, Any]:
    """
    Backfill canonical FundingPips state for users that only have legacy prop_accounts.

    Compatibility behavior is intentionally preserved: legacy rows remain the source of
    truth for historical extension flows while canonical rows are created on demand.
    """
    normalized_uid = str(telegram_user_id or "").strip()
    if not normalized_uid:
        return {
            "user_id": "",
            "trigger": trigger,
            "legacy_account_count": 0,
            "created_trading_accounts": 0,
            "connector_lifecycle_updated": False,
        }

    async with engine.connect() as conn:
        legacy_rows = (
            await conn.execute(
                text(
                    """
                    SELECT account_id, broker, account_type, account_size, label, created_at
                    FROM prop_accounts
                    WHERE telegram_user_id = :uid
                      AND is_active = TRUE
                      AND LOWER(COALESCE(broker, 'fundingpips')) = 'fundingpips'
                    ORDER BY created_at ASC, account_id ASC
                    """
                ),
                {"uid": normalized_uid},
            )
        ).mappings().all()
        canonical_rows = (
            await conn.execute(
                text(
                    """
                    SELECT external_account_id
                    FROM trading_accounts
                    WHERE user_id = :uid
                      AND connector_type = :connector_type
                      AND is_active = TRUE
                    """
                ),
                {"uid": normalized_uid, "connector_type": FUNDINGPIPS_CONNECTOR_TYPE},
            )
        ).mappings().all()

    legacy_accounts = [dict(row) for row in legacy_rows]
    canonical_external_ids = {
        str(row.get("external_account_id") or "").strip()
        for row in canonical_rows
        if str(row.get("external_account_id") or "").strip()
    }

    missing_legacy_accounts = [
        row
        for row in legacy_accounts
        if str(row.get("account_id") or "").strip()
        and (str(row.get("broker") or "fundingpips").strip().lower() == "fundingpips")
        and str(row.get("account_id") or "").strip() not in canonical_external_ids
    ]

    created_trading_accounts = 0
    latest_activity_at: datetime | None = None

    for row in missing_legacy_accounts:
        account_id = str(row.get("account_id") or "").strip()
        if not account_id:
            continue
        await upsert_trading_account(
            {
                "user_id": normalized_uid,
                "connector_type": FUNDINGPIPS_CONNECTOR_TYPE,
                "broker_name": row.get("broker") or "fundingpips",
                "external_account_id": account_id,
                "display_label": row.get("label"),
                "account_type": row.get("account_type"),
                "account_size": row.get("account_size"),
                "metadata": {
                    "compat_source": "prop_accounts_hydration",
                    "hydrated_from_legacy": True,
                    "hydration_trigger": trigger,
                },
            }
        )
        created_trading_accounts += 1

        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            latest_activity_at = created_at if latest_activity_at is None else max(latest_activity_at, created_at)

    lifecycle_updated = False
    if legacy_accounts:
        lifecycle_row = await get_connector_lifecycle(normalized_uid, FUNDINGPIPS_CONNECTOR_TYPE)
        status = str((lifecycle_row or {}).get("status") or "").strip().lower()
        is_connected = (lifecycle_row or {}).get("is_connected")
        requires_normalization = lifecycle_row is None or status in {"", "disconnected"} or is_connected is False
        if requires_normalization:
            await upsert_connector_lifecycle(
                user_id=normalized_uid,
                connector_type=FUNDINGPIPS_CONNECTOR_TYPE,
                status="connected",
                is_connected=True,
                last_activity_at=latest_activity_at or datetime.now(timezone.utc),
                metadata={
                    "source": "fundingpips_hydration",
                    "hydration_trigger": trigger,
                    "legacy_account_count": len(legacy_accounts),
                },
            )
            lifecycle_updated = True

    if created_trading_accounts > 0:
        logger.info(
            "fundingpips_canonical_hydration event=legacy_to_canonical user_id=%s trigger=%s legacy_accounts=%s created_trading_accounts=%s lifecycle_updated=%s",
            normalized_uid,
            trigger,
            len(legacy_accounts),
            created_trading_accounts,
            lifecycle_updated,
        )

    return {
        "user_id": normalized_uid,
        "trigger": trigger,
        "legacy_account_count": len(legacy_accounts),
        "created_trading_accounts": created_trading_accounts,
        "connector_lifecycle_updated": lifecycle_updated,
    }
