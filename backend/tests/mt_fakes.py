"""Shared test doubles for the Match-Trader API integration: a scripted fake broker served
through httpx.MockTransport. No network access, no real credentials."""
import base64
import json
import os

import httpx

TOKEN_A = "fake-mt-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-a1b2"
TOKEN_B = "fake-mt-token-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB-c3d4"


def configure_keys(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_TOKEN_KEYS", json.dumps({"v1": base64.b64encode(os.urandom(32)).decode()}))
    monkeypatch.setenv("TALITRADE_MT_TOKEN_KEY_ACTIVE", "v1")


def closed(uid, position_id="P1", close_time=1_700_000_000_000, net="10.5", symbol="US30", side="BUY", volume="0.1", **extra):
    return {"uid": uid, "id": position_id, "closingOrderId": f"O{uid}", "symbol": symbol, "alias": symbol, "comment": "", "closeReason": "USER",
            "side": side, "volume": float(volume), "openPrice": 39000.0, "closePrice": 39050.5, "commission": -0.5, "swap": 0.0,
            "profit": float(net) + 0.5, "netProfit": float(net), "openTime": close_time - 60_000, "closeTime": close_time,
            "slPrice": None, "tpPrice": None, **extra}


def position(pid, symbol="US30", side="BUY", **extra):
    return {"id": pid, "symbol": symbol, "alias": symbol, "comment": "", "side": side, "volume": 0.1, "openPrice": 39000.0, "currentPrice": 39010.0,
            "commission": -0.5, "swap": 0.0, "profit": 1.0, "netProfit": 0.5, "margin": 20.0, "openTime": 1_700_000_000_000,
            "tpPrice": None, "slPrice": None, "trailingDistance": None, **extra}


class FakeBroker:
    """One fake trading account per token."""

    def __init__(self):
        self.accounts = {}
        self.requests = []          # (method, path, params, token)
        self.fail = {}              # path -> (status, headers) applied to every token
        self.add_account(TOKEN_A, login="700001", platform_url="https://mtr.broker-one.example")

    def add_account(self, token, *, login, platform_url, broker_name="Broker One"):
        self.accounts[token] = {
            "details": {"brokerName": broker_name, "platformUrl": platform_url},
            "account": {"login": login, "email": "trader@example.com", "firstName": "T", "lastName": "R", "currency": "USD", "accountType": "DEMO",
                        "currencyPrecision": 2, "leverage": 100, "createTime": 1_600_000_000_000, "retail": True, "accessRight": "FULL"},
            "summary": {"balance": 10000.12, "credit": 0, "equity": 10010.62, "margin": 0, "freeFunds": 10010.62, "marginLevel": 0, "profit": 10.5},
            "positions": [], "pending": [], "closed": [], "ledgers": [],
        }
        return self.accounts[token]

    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path
        params = dict(request.url.params)
        self.requests.append((request.method, path, params, token))
        if request.method != "GET":
            return httpx.Response(405, json={"type": "method-not-allowed"})
        if path in self.fail:
            status, headers = self.fail[path]
            return httpx.Response(status, headers=headers, json={"type": f"https://example/problems/{status}", "detail": "prose that must not be parsed"})
        account = self.accounts.get(token)
        if account is None:
            return httpx.Response(401, json={"type": "https://example/problems/unauthorized"})
        if path == "/v1/platform-details":
            return httpx.Response(200, json=account["details"])
        if path == "/v1/account":
            return httpx.Response(200, json=account["account"])
        if path == "/v1/account-summary":
            return httpx.Response(200, json=account["summary"])
        if path == "/v1/positions":
            return httpx.Response(200, json={"positions": account["positions"]})
        if path == "/v1/pending-orders":
            return httpx.Response(200, json={"pendingOrders": account["pending"]})
        if path in ("/v1/closed-positions", "/v1/ledgers"):
            lo, hi, limit = int(params["from"]), int(params["to"]), int(params.get("limit", 100))
            key, time_field, envelope = ("closed", "closeTime", "closedPositions") if path == "/v1/closed-positions" else ("ledgers", "timestamp", "ledgers")
            rows = sorted((r for r in account[key] if lo <= r[time_field] <= hi), key=lambda r: r[time_field], reverse=True)
            return httpx.Response(200, json={envelope: rows[:limit]})  # newest first, capped, no cursor
        return httpx.Response(404, json={"type": "not-found"})

    def transport(self):
        return httpx.MockTransport(self.handler)

    def calls(self, path=None):
        return [r for r in self.requests if path is None or r[1] == path]
