"""
FundingPips Prop Firm Connector
================================
Authenticates against the FundingPips portal using the user's email/password,
discovers all trading accounts under that login, extracts MatchTrader credentials,
and stores everything encrypted via secret_crypto.py.

Flow:
  1. POST /auth/login → get FP session token
  2. GET /accounts    → list funded accounts + MatchTrader credentials
  3. Upsert each account into trading_accounts with platform_credentials encrypted
  4. Upsert connector_lifecycle as CONNECTED

Usage (from main.py route):
  from app.services.fundingpips_connector import connect_fundingpips_prop_firm

  result = await connect_fundingpips_prop_firm(
      user_id=resolved_uid,
      email=payload.email,
      password=payload.password,
      label=payload.label,
  )
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.services.connector_ingest import (
    upsert_connector_lifecycle,
    upsert_trading_account,
)
from app.services.secret_crypto import encrypt_secret, decrypt_secret

logger = logging.getLogger(__name__)

# FundingPips internal API base — same endpoints the extension was scraping
FP_API_BASE = "https://api.fundingpips.com"
FP_MTR_BASE = "https://mtr-platform.fundingpips.com"

CONNECTOR_TYPE = "fundingpips_prop"


class FundingPipsAuthError(Exception):
    """Raised when FundingPips credentials are invalid or session cannot be established."""


class FundingPipsAccountDiscoveryError(Exception):
    """Raised when account discovery fails after a valid auth."""


async def _fp_authenticate(email: str, password: str) -> dict[str, Any]:
    """
    POST to FundingPips auth endpoint.
    Returns the session payload including access_token and user info.
    Raises FundingPipsAuthError on failure.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            res = await client.post(
                f"{FP_API_BASE}/auth/login",
                json={"email": email, "password": password},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise FundingPipsAuthError(f"Network error reaching FundingPips: {exc}") from exc

        if res.status_code == 401:
            raise FundingPipsAuthError("Invalid FundingPips credentials. Check email and password.")
        if res.status_code == 422:
            raise FundingPipsAuthError("FundingPips rejected the login payload (validation error).")
        if res.status_code >= 500:
            raise FundingPipsAuthError(f"FundingPips server error ({res.status_code}). Try again later.")
        if res.status_code != 200:
            raise FundingPipsAuthError(f"Unexpected FundingPips auth response: {res.status_code}")

        try:
            data = res.json()
        except Exception as exc:
            raise FundingPipsAuthError("FundingPips returned invalid JSON on auth.") from exc

        token = data.get("access_token") or data.get("token") or data.get("data", {}).get("access_token")
        if not token:
            raise FundingPipsAuthError("FundingPips auth succeeded but returned no access token.")

        return {"access_token": token, "user": data.get("user") or data.get("data", {}).get("user") or {}}


async def _fp_discover_accounts(access_token: str) -> list[dict[str, Any]]:
    """
    Fetch all trading accounts associated with this FundingPips session.
    Each account includes MatchTrader login credentials.
    Returns a list of normalised account dicts.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        # Primary accounts endpoint
        try:
            res = await client.get(f"{FP_API_BASE}/accounts", headers=headers)
        except httpx.RequestError as exc:
            raise FundingPipsAccountDiscoveryError(f"Network error fetching FP accounts: {exc}") from exc

        if res.status_code == 401:
            raise FundingPipsAuthError("FundingPips session expired during account discovery.")
        if res.status_code != 200:
            raise FundingPipsAccountDiscoveryError(
                f"FundingPips accounts endpoint returned {res.status_code}"
            )

        try:
            data = res.json()
        except Exception as exc:
            raise FundingPipsAccountDiscoveryError("FundingPips accounts returned invalid JSON.") from exc

        # FP returns accounts in various shapes — normalise them
        raw_accounts = (
            data.get("data")
            or data.get("accounts")
            or (data if isinstance(data, list) else [])
        )
        if not isinstance(raw_accounts, list):
            raw_accounts = []

        accounts = []
        for acct in raw_accounts:
            account_id = str(
                acct.get("account_login")
                or acct.get("login")
                or acct.get("id")
                or acct.get("account_id")
                or ""
            ).strip()
            if not account_id:
                continue

            # MatchTrader credentials are embedded in the FP account object
            mt_login = str(acct.get("mt_login") or acct.get("login") or account_id)
            mt_server = str(
                acct.get("mt_server")
                or acct.get("server")
                or acct.get("trading_server")
                or "MatchTrader"
            )
            mt_password = str(acct.get("mt_password") or acct.get("password") or "")

            accounts.append({
                "account_id": account_id,
                "display_label": (
                    acct.get("label")
                    or acct.get("name")
                    or acct.get("account_name")
                    or f"FundingPips {account_id}"
                ),
                "account_type": (
                    acct.get("challenge_type")
                    or acct.get("account_type")
                    or acct.get("phase")
                    or "funded"
                ),
                "account_size": int(acct.get("balance") or acct.get("account_size") or 0) or None,
                "phase": acct.get("phase") or acct.get("challenge_type") or "master",
                "is_funded": bool(acct.get("is_funded") or acct.get("funded")),
                "payout_eligible": bool(acct.get("payout_eligible")),
                "platform": "matchtrade",
                "platform_credentials": {
                    "mt_login": mt_login,
                    "mt_server": mt_server,
                    "mt_password": mt_password,  # encrypted before storage
                },
                "raw": acct,  # keep raw for debugging, not stored
            })

        return accounts


async def connect_fundingpips_prop_firm(
    user_id: str,
    email: str,
    password: str,
    label: str | None = None,
) -> dict[str, Any]:
    """
    Full prop firm connector flow for FundingPips.

    1. Authenticate → get FP session token
    2. Discover accounts → get MatchTrader credentials per account
    3. Encrypt and store platform credentials
    4. Upsert trading_accounts (one row per FP account)
    5. Upsert connector_lifecycle as connected
    6. Return summary

    Raises:
        FundingPipsAuthError: bad credentials or session failure
        FundingPipsAccountDiscoveryError: auth ok but couldn't list accounts
    """
    logger.info("FundingPips connector: authenticating user=%s email=%s", user_id, email)

    # Step 1: Authenticate
    auth = await _fp_authenticate(email, password)
    access_token = auth["access_token"]
    fp_user = auth["user"]

    logger.info("FundingPips connector: auth ok, discovering accounts user=%s", user_id)

    # Step 2: Discover accounts
    accounts = await _fp_discover_accounts(access_token)

    if not accounts:
        # Auth worked but no accounts found — still a valid connection, just empty
        logger.warning("FundingPips connector: auth ok but no accounts found user=%s", user_id)

    logger.info(
        "FundingPips connector: discovered %d accounts user=%s", len(accounts), user_id
    )

    # Step 3 + 4: Encrypt credentials and upsert each trading account
    upserted_accounts = []
    for acct in accounts:
        creds = acct["platform_credentials"]

        # Encrypt the MatchTrader password before storage
        encrypted_password = encrypt_secret(creds["mt_password"]) if creds["mt_password"] else None

        trading_account = await upsert_trading_account({
            "user_id": user_id,
            "connector_type": CONNECTOR_TYPE,
            "broker_name": "fundingpips",
            "external_account_id": acct["account_id"],
            "display_label": label or acct["display_label"],
            "account_type": acct["account_type"],
            "account_size": acct["account_size"],
            "is_active": True,
            "metadata": {
                "platform": "matchtrade",
                "fp_phase": acct["phase"],
                "is_funded": acct["is_funded"],
                "payout_eligible": acct["payout_eligible"],
                "provider_state": "connected",
                "last_validated_at": datetime.now(timezone.utc).isoformat(),
                # Platform credentials stored in metadata (password encrypted)
                "platform_credentials": {
                    "mt_login": creds["mt_login"],
                    "mt_server": creds["mt_server"],
                    "mt_password_encrypted": encrypted_password,
                },
            },
        })
        upserted_accounts.append(trading_account)

    # Step 5: Upsert connector lifecycle
    lifecycle = await upsert_connector_lifecycle(
        user_id=user_id,
        connector_type=CONNECTOR_TYPE,
        status="connected",
        is_connected=True,
        last_activity_at=datetime.now(timezone.utc),
        metadata={
            "action": "prop_firm_connect",
            "provider": "fundingpips",
            "account_count": len(upserted_accounts),
            "fp_user_id": fp_user.get("id") or fp_user.get("user_id"),
            "last_connected_at": datetime.now(timezone.utc).isoformat(),
            # Store encrypted FP session for reconnect/refresh
            "fp_session_encrypted": encrypt_secret(access_token),
        },
    )

    logger.info(
        "FundingPips connector: connected %d accounts user=%s", len(upserted_accounts), user_id
    )

    return {
        "ok": True,
        "provider": "fundingpips",
        "connector_type": CONNECTOR_TYPE,
        "account_count": len(upserted_accounts),
        "accounts": [
            {
                "id": a["id"],
                "external_account_id": a["external_account_id"],
                "display_label": a["display_label"],
                "account_type": a["account_type"],
                "account_size": a["account_size"],
                "provider_state": "connected",
            }
            for a in upserted_accounts
        ],
        "lifecycle": lifecycle,
        "status": "connected",
    }


async def get_fundingpips_platform_credentials(
    user_id: str,
    external_account_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve and decrypt MatchTrader credentials for a connected FundingPips account.
    Used by the MatchTrader provider to establish live data connections.
    Returns None if not found.
    """
    from app.core.database import engine
    from sqlalchemy import text

    async with engine.connect() as conn:
        result = await conn.execute(
            text("""
                SELECT metadata
                FROM trading_accounts
                WHERE user_id = :uid
                  AND connector_type = :connector
                  AND external_account_id = :acct_id
                  AND is_active = TRUE
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            {
                "uid": user_id,
                "connector": CONNECTOR_TYPE,
                "acct_id": external_account_id,
            },
        )
        row = result.mappings().first()

    if not row:
        return None

    metadata = dict(row.get("metadata") or {})
    platform_creds = metadata.get("platform_credentials") or {}

    encrypted_password = platform_creds.get("mt_password_encrypted")
    mt_password = decrypt_secret(encrypted_password) if encrypted_password else None

    return {
        "mt_login": platform_creds.get("mt_login"),
        "mt_server": platform_creds.get("mt_server"),
        "mt_password": mt_password,
        "platform": "matchtrade",
    }
