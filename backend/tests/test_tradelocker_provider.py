import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.tradelocker_provider import TradeLockerApiError, TradeLockerAuthError, TradeLockerClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, headers=None, json=None):
        if not self._responses:
            return FakeResponse()
        return self._responses.pop(0)


def test_login_tolerates_wrapped_payload(monkeypatch):
    wrapped = {"s": "ok", "d": {"accessToken": "a", "refreshToken": "r"}}
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(payload=wrapped)]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    result = asyncio.run(client.login_password(email="e", password="p"))
    assert result["access_token"] == "a"
    assert result["refresh_token"] == "r"


def test_positions_tolerate_array_rows(monkeypatch):
    payload = {"s": "ok", "d": [[1, 401, "buy", 0.5, 123.4, 5.6, "2026-01-01T00:00:00Z"]]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(payload=payload)]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    rows = asyncio.run(client.get_positions("token", "acct"))
    assert rows[0]["tradableInstrumentId"] == 401
    assert rows[0]["qty"] == 0.5


def test_history_tolerates_array_rows(monkeypatch):
    payload = {"s": "ok", "d": [[10, 777, "sell", 1.2, 100.0, 99.0, -1.0, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "deal", 0.5]]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(payload=payload)]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    rows = asyncio.run(client.get_order_history("token", "acct"))
    assert rows[0]["entryPrice"] == 100.0
    assert rows[0]["exitPrice"] == 99.0


def test_accounts_payload_shape_error(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(payload={"s": "ok", "d": {"unexpected": True}})]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    with pytest.raises(TradeLockerApiError) as exc:
        asyncio.run(client.list_accounts("token"))
    assert str(exc.value) == "unexpected_accounts_payload"


def test_login_maps_unauthorized_to_invalid_credentials(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(status_code=401, payload={"error": "bad creds"})]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    with pytest.raises(TradeLockerAuthError) as exc:
        asyncio.run(client.login_password(email="e", password="bad"))
    assert str(exc.value) == "invalid_credentials"


def test_refresh_preserves_unauthorized_error(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=20.0: FakeClient([FakeResponse(status_code=401, payload={"error": "expired"})]))
    client = TradeLockerClient(base_url="https://tl.example.com")

    with pytest.raises(TradeLockerAuthError) as exc:
        asyncio.run(client.refresh_token("stale-refresh"))
    assert str(exc.value) == "unauthorized"
