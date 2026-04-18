from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


TRADINGVIEW_CONNECTOR = "tradingview_webhook"
MAX_MESSAGE_LENGTH = 512
MAX_SYMBOL_LENGTH = 64
MAX_TIMEFRAME_LENGTH = 32
MAX_EVENT_TYPE_LENGTH = 64
MAX_RAW_PAYLOAD_KEYS = 40

SENSITIVE_KEYS = {
    "token",
    "webhook_token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
}


def _to_safe_text(value: Any, *, max_len: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_len]


def _extract_first(payload: dict[str, Any], keys: tuple[str, ...], *, max_len: int) -> str | None:
    for key in keys:
        if key in payload:
            text = _to_safe_text(payload.get(key), max_len=max_len)
            if text:
                return text
    return None


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if len(safe) >= MAX_RAW_PAYLOAD_KEYS:
            break
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if key_text.lower() in SENSITIVE_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key_text[:64]] = value
        else:
            safe[key_text[:64]] = str(value)[:MAX_MESSAGE_LENGTH]
    return safe


def normalize_tradingview_event(
    *,
    user_id: str,
    trading_account_id: int,
    account_key: str | None,
    connection_id: int,
    payload: dict[str, Any],
    received_at: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("invalid_payload")

    now = received_at or datetime.now(timezone.utc)
    event_type = _extract_first(payload, ("event_type", "type", "action", "event"), max_len=MAX_EVENT_TYPE_LENGTH) or "alert"
    symbol = _extract_first(payload, ("symbol", "ticker", "instrument"), max_len=MAX_SYMBOL_LENGTH)
    timeframe = _extract_first(payload, ("timeframe", "interval", "tf"), max_len=MAX_TIMEFRAME_LENGTH)
    title = _extract_first(payload, ("title", "alert_name", "name"), max_len=MAX_MESSAGE_LENGTH)
    message = _extract_first(payload, ("message", "text", "note", "comment"), max_len=MAX_MESSAGE_LENGTH)

    safe_payload = _sanitize_payload(payload)
    if not (symbol or timeframe or title or message or safe_payload):
        raise ValueError("invalid_payload")

    return {
        "user_id": str(user_id),
        "connector_type": TRADINGVIEW_CONNECTOR,
        "trading_account_id": int(trading_account_id),
        "external_connection_id": f"tv_conn_{connection_id}",
        "account_key": str(account_key or "").strip() or None,
        "event_type": event_type,
        "symbol": symbol,
        "timeframe": timeframe,
        "title": title,
        "message": message,
        "raw_payload_json": safe_payload,
        "received_at": now,
        "is_valid": True,
    }
