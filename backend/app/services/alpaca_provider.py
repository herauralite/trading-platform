from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

ALPACA_ENVIRONMENTS = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}


def normalize_alpaca_environment(environment: str | None) -> str:
    normalized = str(environment or "paper").strip().lower()
    return normalized if normalized in ALPACA_ENVIRONMENTS else "paper"


def _to_decimal_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


async def validate_alpaca_credentials(
    *,
    environment: str,
    api_key: str,
    api_secret: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    normalized_env = normalize_alpaca_environment(environment)
    base_url = ALPACA_ENVIRONMENTS[normalized_env]
    key = str(api_key or "").strip()
    secret = str(api_secret or "").strip()
    if not key or not secret:
        raise ValueError("invalid_credentials")

    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(f"{base_url}/v2/account", headers=headers)

    if response.status_code == 401:
        raise ValueError("invalid_credentials")
    response.raise_for_status()

    payload = response.json() if response.content else {}
    account_number = str(payload.get("account_number") or payload.get("id") or "").strip()
    if not account_number:
        raise ValueError("invalid_account_payload")

    return {
        "provider_state": f"{normalized_env}_connected",
        "environment": normalized_env,
        "alpaca_account_number": account_number,
        "alpaca_status": payload.get("status"),
        "currency": payload.get("currency"),
        "cash": _to_decimal_or_none(payload.get("cash")),
        "equity": _to_decimal_or_none(payload.get("equity")),
        "portfolio_value": _to_decimal_or_none(payload.get("portfolio_value")),
        "buying_power": _to_decimal_or_none(payload.get("buying_power")),
        "pattern_day_trader": bool(payload.get("pattern_day_trader")),
        "trading_blocked": bool(payload.get("trading_blocked")),
        "raw_account_id": payload.get("id"),
    }
