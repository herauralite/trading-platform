from datetime import datetime, timezone

import pytest

from app.services.tradingview_events import normalize_tradingview_event


def test_normalize_tradingview_event_keeps_safe_fields():
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    event = normalize_tradingview_event(
        user_id="u-1",
        trading_account_id=7,
        account_key="tradingview_webhook:u-1:abc",
        connection_id=11,
        payload={
            "symbol": "BTCUSDT",
            "timeframe": "15",
            "title": "Breakout",
            "message": "Long trigger",
            "token": "should_not_persist",
            "nested": {"x": 1},
        },
        received_at=now,
    )
    assert event["connector_type"] == "tradingview_webhook"
    assert event["event_type"] == "alert"
    assert event["symbol"] == "BTCUSDT"
    assert event["timeframe"] == "15"
    assert event["title"] == "Breakout"
    assert event["message"] == "Long trigger"
    assert event["raw_payload_json"]["symbol"] == "BTCUSDT"
    assert "token" not in event["raw_payload_json"]
    assert event["raw_payload_json"]["nested"].startswith("{")


def test_normalize_tradingview_event_rejects_invalid_payload():
    with pytest.raises(ValueError, match="invalid_payload"):
        normalize_tradingview_event(
            user_id="u-1",
            trading_account_id=7,
            account_key=None,
            connection_id=11,
            payload={},
        )
