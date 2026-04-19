from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _pick(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def _as_dict(value: Any, *, error_code: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise TradeLockerApiError(error_code)


def _as_list(value: Any, *, error_code: str) -> list[Any]:
    if isinstance(value, list):
        return value
    raise TradeLockerApiError(error_code)


def _unwrap_data(payload: Any) -> Any:
    """
    TradeLocker responses may be root payloads or wrapped with {"s": "...", "d": ...}.
    """
    if isinstance(payload, dict) and "d" in payload:
        return payload.get("d")
    return payload


def _normalize_position_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if isinstance(row, list):
        # Best-effort for common positional payloads.
        return {
            "id": row[0] if len(row) > 0 else None,
            "tradableInstrumentId": row[1] if len(row) > 1 else None,
            "side": row[2] if len(row) > 2 else None,
            "qty": row[3] if len(row) > 3 else None,
            "avgPrice": row[4] if len(row) > 4 else None,
            "unrealizedPnl": row[5] if len(row) > 5 else None,
            "openTime": row[6] if len(row) > 6 else None,
        }
    raise TradeLockerApiError("unexpected_position_row_shape")


def _normalize_history_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if isinstance(row, list):
        # Best-effort for common positional payloads.
        return {
            "id": row[0] if len(row) > 0 else None,
            "tradableInstrumentId": row[1] if len(row) > 1 else None,
            "side": row[2] if len(row) > 2 else None,
            "qty": row[3] if len(row) > 3 else None,
            "entryPrice": row[4] if len(row) > 4 else None,
            "exitPrice": row[5] if len(row) > 5 else None,
            "realizedPnl": row[6] if len(row) > 6 else None,
            "openTime": row[7] if len(row) > 7 else None,
            "closeTime": row[8] if len(row) > 8 else None,
            "type": row[9] if len(row) > 9 else None,
            "commission": row[10] if len(row) > 10 else None,
        }
    raise TradeLockerApiError("unexpected_history_row_shape")


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
    ) -> Any:
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
        try:
            return response.json()
        except Exception as exc:
            raise TradeLockerApiError("non_json_response") from exc

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
        raw = await self._request(method="POST", path="/auth/jwt/token", json_body=payload, allow_401=True)
        data_root = _as_dict(raw, error_code="invalid_login_payload")
        data = _unwrap_data(data_root)
        if not isinstance(data, dict):
            raise TradeLockerAuthError("invalid_login_payload")
        access_token = str(_pick(data.get("accessToken"), data.get("access_token"), data_root.get("accessToken"), data_root.get("access_token")) or "").strip()
        refresh_token = str(_pick(data.get("refreshToken"), data.get("refresh_token"), data_root.get("refreshToken"), data_root.get("refresh_token")) or "").strip()
        if not access_token or not refresh_token:
            raise TradeLockerAuthError("invalid_login_payload")
        expires_raw = _pick(
            data.get("accessTokenExpiresAt"),
            data.get("access_token_expires_at"),
            data_root.get("accessTokenExpiresAt"),
            data_root.get("access_token_expires_at"),
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": parse_expiry(expires_raw),
        }

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        raw = await self._request(
            method="POST",
            path="/auth/jwt/refresh",
            json_body={"refreshToken": refresh_token},
            allow_401=True,
        )
        data_root = _as_dict(raw, error_code="invalid_refresh_payload")
        data = _unwrap_data(data_root)
        if not isinstance(data, dict):
            raise TradeLockerAuthError("invalid_refresh_payload")
        access_token = str(_pick(data.get("accessToken"), data.get("access_token"), data_root.get("accessToken"), data_root.get("access_token")) or "").strip()
        next_refresh = str(_pick(data.get("refreshToken"), data.get("refresh_token"), data_root.get("refreshToken"), data_root.get("refresh_token"), refresh_token) or "").strip()
        if not access_token:
            raise TradeLockerAuthError("invalid_refresh_payload")
        expires_raw = _pick(
            data.get("accessTokenExpiresAt"),
            data.get("access_token_expires_at"),
            data_root.get("accessTokenExpiresAt"),
            data_root.get("access_token_expires_at"),
        )
        return {
            "access_token": access_token,
            "refresh_token": next_refresh,
            "expires_at": parse_expiry(expires_raw),
        }

    async def list_accounts(self, access_token: str) -> list[dict[str, Any]]:
        raw = await self._request(method="GET", path="/trade/accounts", token=access_token, allow_401=True)
        data = _unwrap_data(raw)
        if isinstance(data, dict):
            rows = _pick(data.get("accounts"), data.get("rows"), data.get("items"))
        else:
            rows = data
        rows = _as_list(rows, error_code="unexpected_accounts_payload")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise TradeLockerApiError("unexpected_accounts_row_shape")
            normalized.append(row)
        return normalized

    async def get_account(self, access_token: str, account_id: str) -> dict[str, Any]:
        raw = await self._request(method="GET", path=f"/trade/accounts/{account_id}", token=access_token, allow_401=True)
        data = _unwrap_data(raw)
        if not isinstance(data, dict):
            raise TradeLockerApiError("unexpected_account_payload")
        return data

    async def get_positions(self, access_token: str, account_id: str) -> list[dict[str, Any]]:
        raw = await self._request(method="GET", path=f"/trade/accounts/{account_id}/positions", token=access_token, allow_401=True)
        data = _unwrap_data(raw)
        if isinstance(data, dict):
            rows = _pick(data.get("positions"), data.get("rows"), data.get("items"))
        else:
            rows = data
        rows = _as_list(rows, error_code="unexpected_positions_payload")
        return [_normalize_position_row(row) for row in rows]

    async def get_instruments(self, access_token: str) -> dict[str, str]:
        raw = await self._request(method="GET", path="/trade/config/instruments", token=access_token, allow_401=True)
        data = _unwrap_data(raw)
        if isinstance(data, dict):
            rows = _pick(data.get("instruments"), data.get("rows"), data.get("items"))
        else:
            rows = data
        rows = _as_list(rows, error_code="unexpected_instruments_payload")
        symbols: dict[str, str] = {}
        for row in rows:
            item = _as_dict(row, error_code="unexpected_instruments_row_shape")
            instrument_id = str(_pick(item.get("tradableInstrumentId"), item.get("id")) or "").strip()
            symbol = str(_pick(item.get("symbol"), item.get("name")) or "").strip()
            if instrument_id and symbol:
                symbols[instrument_id] = symbol
        return symbols

    async def get_order_history(self, access_token: str, account_id: str) -> list[dict[str, Any]]:
        raw = await self._request(method="GET", path=f"/trade/accounts/{account_id}/history", token=access_token, allow_401=True)
        data = _unwrap_data(raw)
        if isinstance(data, dict):
            rows = _pick(data.get("history"), data.get("orders"), data.get("rows"), data.get("items"))
        else:
            rows = data
        rows = _as_list(rows, error_code="unexpected_history_payload")
        return [_normalize_history_row(row) for row in rows]
