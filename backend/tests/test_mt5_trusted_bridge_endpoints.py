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


def test_mt5_pairing_token_endpoint(monkeypatch):
    async def fake_create_mt5_pairing_token(**kwargs):
        assert kwargs["user_id"] == "u-token"
        return {"pairing_token": "mtpair_abc", "expires_at": "2026-04-18T12:00:00Z", "status": "pending"}

    async def fake_get_registration(user_id):
        assert user_id == "u-token"
        return {"active_bridge_count": 0, "bridges": [], "pending_pairing_token": {"token_hint": "mtpair…_abc"}}

    monkeypatch.setattr("app.main.create_mt5_pairing_token", fake_create_mt5_pairing_token)
    monkeypatch.setattr("app.main.get_user_bridge_registration_state", fake_get_registration)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-token")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/connectors/mt5_bridge/pairing/token",
                json={"external_account_id": "acct-1", "mt5_server": "MetaQuotes"},
                headers={"Authorization": f"Bearer {token}"},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pairing"]["pairing_token"] == "mtpair_abc"
    assert body["registration"]["active_bridge_count"] == 0


def test_mt5_bridge_register_and_heartbeat_endpoints(monkeypatch):
    async def fake_register(**kwargs):
        assert kwargs["pairing_token"] == "mtpair_real"
        return {"bridge_id": "bridge_1", "bridge_secret": "bridgesecret_1", "status": "registered"}

    async def fake_heartbeat(**kwargs):
        assert kwargs["bridge_id"] == "bridge_1"
        assert kwargs["bridge_secret"] == "bridgesecret_1"
        return {"bridge_id": "bridge_1", "status": "online", "last_heartbeat_at": "2026-04-18T12:05:00Z"}

    monkeypatch.setattr("app.main.register_mt5_trusted_bridge", fake_register)
    monkeypatch.setattr("app.main.heartbeat_mt5_trusted_bridge", fake_heartbeat)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            register_resp = await client.post(
                "/connectors/mt5_bridge/bridges/register",
                json={"pairing_token": "mtpair_real", "machine_label": "worker-a"},
            )
            heartbeat_resp = await client.post(
                "/connectors/mt5_bridge/bridges/heartbeat",
                json={"bridge_id": "bridge_1", "bridge_secret": "bridgesecret_1", "status": "online"},
            )
            return register_resp, heartbeat_resp

    register_resp, heartbeat_resp = asyncio.run(_run())
    assert register_resp.status_code == 200
    assert register_resp.json()["bridge"]["bridge_id"] == "bridge_1"
    assert heartbeat_resp.status_code == 200
    assert heartbeat_resp.json()["bridge"]["status"] == "online"
