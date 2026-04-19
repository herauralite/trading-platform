from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import httpx


class TradeLockerAuthError(ValueError):
    pass


class TradeLockerApiError(RuntimeError):
    pass


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_expiry(expires_at_raw: Any, *, fallback_seconds: int = 900) -> datetime:
    parsed = _parse_dt(expires_at_raw)
    if parsed:
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc) + timedelta(seconds=max(60, fallback_seconds))


def token_is_expiring_soon(expires_at_raw: Any, *, skew_seconds: int = 60) -> bool:
    expires_at = _parse_dt(expires_at_raw)
    if not expires_at:
        return True
    return expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(seconds=skew_seconds)


class TradeLockerClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 20.0) -> None:
        cleaned = str(base_url or "").strip().rstrip("/")
        if not cleaned:
            raise ValueError("missing_base_url")
        self.base_url = cleaned
        self.timeout_seconds = timeout_seconds

    async def _request(
        self,
        *,
        method: str,
        path: str,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        allow_401: bool = False,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
            )
        if response.status_code in {401, 403}:
            if allow_401:
                raise TradeLockerAuthError("unauthorized")
            raise TradeLockerAuthError("invalid_credentials")
        if response.status_code >= 400:
            raise TradeLockerApiError(f"tradelocker_http_{response.status_code}")
        if not response.content:
            return {}
        return response.json()

    async def login_password(
        self,
        *,
        email: str,
        password: str,
        server: str | None = None,
    ) -> dict[str, Any]:
        payload = {"email": email, "password": password}
        if server:
            payload["server"] = server
        data = await self._request(method="POST", path="/auth/jwt/token", json_body=payload, allow_401=True)
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        refresh_token = str(data.get("refreshToken") or data.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise TradeLockerAuthError("invalid_login_payload")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": parse_expiry(data.get("accessTokenExpiresAt") or data.get("access_token_expires_at")),
        }

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        data = await self._request(
            method="POST",
            path="/auth/jwt/refresh",
            json_body={"refreshToken": refresh_token},
            allow_401=True,
        )
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        next_refresh = str(data.get("refreshToken") or data.get("refresh_token") or refresh_token).strip()
        if not access_token:
            raise TradeLockerAuthError("invalid_refresh_payload")
        return {
            "access_token": access_token,
            "refresh_token": next_refresh,
            "expires_at": parse_expiry(data.get("accessTokenExpiresAt") or data.get("access_token_expires_at")),
        }

    async def get_account(self, access_token: str, account_id: str) -> dict[str, Any]:
        return await self._request(method="GET", path=f"/trade/accounts/{account_id}", token=access_token, allow_401=True)

    async def get_positions(self, access_token: str, account_id: str) -> list[dict[str, Any]]:
        data = await self._request(method="GET", path=f"/trade/accounts/{account_id}/positions", token=access_token, allow_401=True)
        if isinstance(data, list):
            return data
        return list(data.get("positions") or data.get("d") or [])

    async def get_instruments(self, access_token: str) -> dict[str, str]:
        data = await self._request(method="GET", path="/trade/config/instruments", token=access_token, allow_401=True)
        rows = data if isinstance(data, list) else list(data.get("instruments") or data.get("d") or [])
        symbols: dict[str, str] = {}
        for row in rows:
            instrument_id = str(row.get("tradableInstrumentId") or row.get("id") or "").strip()
            symbol = str(row.get("symbol") or row.get("name") or "").strip()
            if instrument_id and symbol:
                symbols[instrument_id] = symbol
        return symbols

    async def get_order_history(self, access_token: str, account_id: str) -> list[dict[str, Any]]:
        data = await self._request(method="GET", path=f"/trade/accounts/{account_id}/history", token=access_token, allow_401=True)
        if isinstance(data, list):
            return data
        return list(data.get("history") or data.get("orders") or data.get("d") or [])
