import asyncio
import os
import sys
import types

import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

if "jwt" not in sys.modules:
    fake_jwt = types.ModuleType("jwt")

    class FakePyJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            class Key:
                key = "stub"
            return Key()

    fake_jwt.PyJWKClient = FakePyJWKClient
    fake_jwt.decode = lambda *args, **kwargs: {}
    sys.modules["jwt"] = fake_jwt

from app.core.auth_session import create_session_token
from app.main import app


def test_catalog_includes_new_provider_foundation():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/catalog")

    response = asyncio.run(_run())
    assert response.status_code == 200
    types_seen = {entry["connector_type"] for entry in response.json()["connectors"]}
    assert "mt5_bridge" in types_seen
    assert "tradingview_webhook" in types_seen
    assert "alpaca_api" in types_seen
    assert "oanda_api" in types_seen
    assert "binance_api" in types_seen


def test_tradingview_create_returns_safe_webhook_metadata(monkeypatch):
    async def fake_create_tradingview_connection(**kwargs):
        assert kwargs["user_id"] == "u-tv"
        return {
            "id": 10,
            "display_label": "TV Signals",
            "activation_state": "awaiting_alerts",
            "created_at": "2026-04-18T12:00:00Z",
            "last_event_at": None,
            "webhook_token_hint": "tvw_1234…abcd",
            "webhook_token": "tvw_real_token",
        }

    monkeypatch.setattr("app.main.create_tradingview_connection", fake_create_tradingview_connection)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-tv")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/tradingview-webhook/connections",
                json={"display_label": "TV Signals"},
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["activation_state"] == "awaiting_alerts"
    assert body["connection"]["webhook_token_hint"].startswith("tvw_")
    assert "tvw_real_token" not in body["connection"]["webhook_token_hint"]
    assert "/webhooks/tradingview/" in body["connection"]["webhook_url"]


def test_tradingview_ingest_transitions_to_active(monkeypatch):
    async def fake_ingest(**kwargs):
        assert kwargs["token"] == "tvw_token"
        return {"ok": True, "state": "active", "event_at": "2026-04-18T12:05:00Z"}

    monkeypatch.setattr("app.main.ingest_tradingview_event", fake_ingest)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/webhooks/tradingview/tvw_token", json={"symbol": "BTCUSDT"})

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert response.json()["state"] == "active"


def test_public_api_beta_shell_stays_waiting_state(monkeypatch):
    async def fake_create_public_api_beta_connection(**kwargs):
        assert kwargs["connector_type"] == "alpaca_api"
        return {
            "id": 55,
            "connector_type": "alpaca_api",
            "display_label": "Alpaca Beta",
            "beta_state": "waiting_for_secure_auth_support",
        }

    monkeypatch.setattr("app.main.create_public_api_beta_connection", fake_create_public_api_beta_connection)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-beta")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/alpaca_api/beta",
                json={"display_label": "Alpaca Beta", "environment": "paper"},
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "waiting_for_secure_auth_support"
    assert body["connection"]["beta_state"] == "waiting_for_secure_auth_support"
