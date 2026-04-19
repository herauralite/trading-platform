import asyncio
from datetime import datetime, timezone
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
import app.services.provider_onboarding as onboarding_mod
from app.services.alpaca_provider import AlpacaCredentialValidationError
from app.services.tradelocker_provider import TradeLockerAuthError
from app.services.secret_crypto import decrypt_secret


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
    assert "tradelocker_api" in types_seen


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


def test_tradingview_ingest_rejects_malformed_json():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/webhooks/tradingview/tvw_token",
                content="{not-json",
                headers={"Content-Type": "application/json"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 400
    assert response.json()["detail"] == "malformed_json"


def test_tradingview_ingest_rejects_invalid_token(monkeypatch):
    async def fake_ingest(**kwargs):
        assert kwargs["token"] == "bad_token"
        raise ValueError("not_found")

    monkeypatch.setattr("app.main.ingest_tradingview_event", fake_ingest)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/webhooks/tradingview/bad_token", json={"symbol": "BTCUSDT"})

    response = asyncio.run(_run())
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


def test_public_api_beta_shell_stays_waiting_state(monkeypatch):
    async def fake_create_public_api_beta_connection(**kwargs):
        assert kwargs["connector_type"] == "oanda_api"
        return {
            "id": 55,
            "connector_type": "oanda_api",
            "display_label": "OANDA Beta",
            "beta_state": "awaiting_secure_auth",
        }

    monkeypatch.setattr("app.main.create_public_api_beta_connection", fake_create_public_api_beta_connection)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-beta")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/alpaca_api/beta",
                json={"display_label": "OANDA Beta", "environment": "paper"},
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 404


def test_alpaca_connect_success_paper(monkeypatch):
    async def fake_connect(**kwargs):
        assert kwargs["environment"] == "paper"
        assert kwargs["api_secret"] == "secret-value"
        return {
            "provider_state": "paper_connected",
            "environment": "paper",
            "validation_error": None,
            "account": {
                "id": 77,
                "display_label": "My Alpaca",
                "environment": "paper",
                "account_summary": {"alpaca_account_number": "PA123"},
                "last_validated_at": "2026-04-18T12:00:00Z",
            },
        }

    monkeypatch.setattr("app.main.connect_alpaca_api_account", fake_connect)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-alpaca")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/alpaca_api/connect",
                json={
                    "label": "My Alpaca",
                    "environment": "paper",
                    "api_key": "key-value",
                    "api_secret": "secret-value",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "paper_connected"
    assert payload["account"]["summary"]["alpaca_account_number"] == "PA123"


def test_alpaca_connect_invalid_credentials_path(monkeypatch):
    async def fake_connect(**kwargs):
        raise AlpacaCredentialValidationError("invalid_credentials")

    monkeypatch.setattr("app.main.connect_alpaca_api_account", fake_connect)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-alpaca")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/alpaca_api/connect",
                json={
                    "label": "My Alpaca",
                    "environment": "paper",
                    "api_key": "bad",
                    "api_secret": "bad",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_credentials"


def test_alpaca_connect_response_never_echoes_raw_secret(monkeypatch):
    async def fake_connect(**kwargs):
        return {
            "provider_state": "paper_connected",
            "environment": "paper",
            "validation_error": None,
            "account": {
                "id": 99,
                "display_label": "My Alpaca",
                "environment": "paper",
                "account_summary": {"alpaca_account_number": "PA999"},
                "last_validated_at": "2026-04-18T12:02:00Z",
            },
        }

    monkeypatch.setattr("app.main.connect_alpaca_api_account", fake_connect)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-alpaca")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/alpaca_api/connect",
                json={
                    "label": "My Alpaca",
                    "environment": "paper",
                    "api_key": "key-value",
                    "api_secret": "super-secret-value",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    serialized = str(response.json())
    assert "super-secret-value" not in serialized


def test_alpaca_secret_storage_is_encrypted(monkeypatch):
    captured_params = {}

    async def fake_validate_alpaca_credentials(**kwargs):
        return {
            "provider_state": "paper_connected",
            "validation_state": "account_verified",
            "environment": "paper",
            "alpaca_account_number": "PA500",
            "alpaca_status": "ACTIVE",
        }

    async def fake_upsert_trading_account(payload):
        return {"id": 501}

    async def fake_upsert_connector_lifecycle(**kwargs):
        return kwargs

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {
                "id": 5,
                "user_id": "u-encrypted",
                "connector_type": "alpaca_api",
                "trading_account_id": 501,
                "display_label": "Enc",
                "environment": "paper",
                "beta_state": "paper_connected",
                "account_summary": {"alpaca_account_number": "PA500"},
                "last_validation_error": None,
                "last_validated_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

    class FakeConn:
        async def execute(self, stmt, params=None):
            captured_params.update(params or {})
            return FakeResult()

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(onboarding_mod, "validate_alpaca_credentials", fake_validate_alpaca_credentials)
    monkeypatch.setattr(onboarding_mod, "upsert_trading_account", fake_upsert_trading_account)
    monkeypatch.setattr(onboarding_mod, "upsert_connector_lifecycle", fake_upsert_connector_lifecycle)
    monkeypatch.setattr(onboarding_mod, "engine", FakeEngine())

    asyncio.run(onboarding_mod.connect_alpaca_api_account(
        user_id="u-encrypted",
        label="Enc",
        environment="paper",
        api_key="raw-api-key",
        api_secret="raw-api-secret",
    ))

    assert captured_params["encrypted_api_key"] != "raw-api-key"
    assert captured_params["encrypted_api_secret"] != "raw-api-secret"
    assert decrypt_secret(captured_params["encrypted_api_key"]) == "raw-api-key"
    assert decrypt_secret(captured_params["encrypted_api_secret"]) == "raw-api-secret"


def test_tradelocker_connect_success(monkeypatch):
    async def fake_connect(**kwargs):
        assert kwargs["account_id"] == "acct-9001"
        return {
            "provider_state": "connected",
            "environment": "demo",
            "validation_error": None,
            "account": {
                "id": 201,
                "display_label": "TL Primary",
                "external_account_id": "acct-9001",
                "last_validated_at": "2026-04-18T12:00:00Z",
            },
        }

    monkeypatch.setattr("app.main.connect_tradelocker_api_account", fake_connect)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-tl")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/tradelocker_api/connect",
                json={
                    "label": "TL Primary",
                    "base_url": "https://tl.example.com",
                    "account_id": "acct-9001",
                    "email": "tl@example.com",
                    "password": "secret",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "tradelocker_api"
    assert payload["status"] == "connected"
    assert payload["account"]["external_account_id"] == "acct-9001"


def test_tradelocker_connect_invalid_credentials(monkeypatch):
    async def fake_connect(**kwargs):
        raise TradeLockerAuthError("invalid_credentials")

    monkeypatch.setattr("app.main.connect_tradelocker_api_account", fake_connect)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-tl")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/providers/public-api/tradelocker_api/connect",
                json={
                    "label": "TL Primary",
                    "base_url": "https://tl.example.com",
                    "account_id": "acct-9001",
                    "email": "tl@example.com",
                    "password": "bad",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    response = asyncio.run(_run())
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_credentials"
