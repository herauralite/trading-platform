import asyncio
import json
import os
import sys

import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.alpaca_provider import AlpacaCredentialValidationError, validate_alpaca_credentials


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.captured_url = None
        self.captured_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        self.captured_url = url
        self.captured_headers = headers
        return self.response


def test_validate_alpaca_credentials_success(monkeypatch):
    response = FakeResponse(payload={
        "id": "acct-id-1",
        "account_number": "PA12345",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "1000.12",
        "equity": "1200.34",
        "portfolio_value": "1234.56",
        "buying_power": "2000.00",
    })
    fake_client = FakeClient(response)

    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=10.0: fake_client)

    result = asyncio.run(validate_alpaca_credentials(
        environment="paper",
        api_key="key",
        api_secret="secret",
    ))

    assert fake_client.captured_url == "https://paper-api.alpaca.markets/v2/account"
    assert result["provider_state"] == "paper_connected"
    assert result["validation_state"] == "account_verified"
    assert result["alpaca_account_number"] == "PA12345"
    assert result["equity"] == 1200.34


def test_validate_alpaca_credentials_invalid_credentials(monkeypatch):
    response = FakeResponse(status_code=401, payload={"message": "unauthorized"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=10.0: FakeClient(response))

    try:
        asyncio.run(validate_alpaca_credentials(
            environment="live",
            api_key="bad",
            api_secret="bad",
        ))
        raised = None
    except AlpacaCredentialValidationError as exc:
        raised = exc

    assert raised is not None
    assert str(raised) == "invalid_credentials"
