from __future__ import annotations

from typing import Any

import httpx


ALPACA_ENVIRONMENTS = {"paper", "live"}
ALPACA_BASE_URLS = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}


class AlpacaValidationError(ValueError):
    pass


def normalize_alpaca_environment(value: str | None) -> str:
    env = str(value or "paper").strip().lower()
    return env if env in ALPACA_ENVIRONMENTS else "paper"


async def validate_alpaca_credentials(
    *,
    api_key: str,
    api_secret: str,
    environment: str | None = None,
) -> dict[str, Any]:
    key = str(api_key or "").strip()
    secret = str(api_secret or "").strip()
    if not key:
        raise AlpacaValidationError("api_key is required")
    if not secret:
        raise AlpacaValidationError("api_secret is required")
    env = normalize_alpaca_environment(environment)
    base_url = ALPACA_BASE_URLS[env]

    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url}/v2/account", headers=headers)
    except httpx.HTTPError as exc:
        raise AlpacaValidationError("Could not reach Alpaca API for validation") from exc

    if response.status_code in {401, 403}:
        raise AlpacaValidationError("Alpaca credentials are invalid for the selected environment")
    if response.status_code >= 400:
        raise AlpacaValidationError(f"Alpaca API validation failed with HTTP {response.status_code}")

    payload = response.json() if "application/json" in (response.headers.get("content-type") or "") else {}
    account_id = str(payload.get("id") or "").strip()
    if not account_id:
        raise AlpacaValidationError("Alpaca response did not include an account id")

    account_status = str(payload.get("status") or "unknown")
    return {
        "environment": env,
        "account_id": account_id,
        "account_number": payload.get("account_number"),
        "account_status": account_status,
        "currency": payload.get("currency"),
        "buying_power": payload.get("buying_power"),
        "equity": payload.get("equity"),
        "last_equity": payload.get("last_equity"),
        "raw": payload,
    }
