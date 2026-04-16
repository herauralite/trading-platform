import os
import sys
import types
import asyncio
import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")

# Test environment can be missing PyJWT. Stub minimally for app import.
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

from app.main import app
from app.services.connector_ingest import compute_account_key


def post_json(path: str, payload: dict):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, json=payload)

    return asyncio.run(_run())


def test_ingest_accounts_route(monkeypatch):
    captured = {}

    async def fake_upsert(payload):
        captured["payload"] = payload
        return {"id": 123, "external_account_id": payload["external_account_id"]}

    monkeypatch.setattr("app.routers.ingest.upsert_trading_account", fake_upsert)

    resp = post_json("/ingest/accounts", {
        "connector_type": "csv_import",
        "user_id": "u1",
        "external_account_id": "acct-1",
        "broker_name": "csv",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["payload"]["external_account_id"] == "acct-1"


def test_ingest_trades_route(monkeypatch):
    async def fake_ingest_trade(payload):
        assert payload["symbol"] == "NAS100"
        assert payload["connector_type"] == "manual"
        return True

    monkeypatch.setattr("app.routers.ingest.ingest_trade", fake_ingest_trade)

    resp = post_json("/ingest/trades", {
        "connector_type": "manual",
        "external_account_id": "acct-2",
        "symbol": "NAS100",
        "side": "buy",
        "pnl": 12.5,
    })
    assert resp.status_code == 200
    assert resp.json()["persisted"] is True


def test_extension_trade_compatibility(monkeypatch):
    captured = {}

    async def fake_ingest_trade(payload):
        captured.update(payload)
        return True

    monkeypatch.setattr("app.main.ingest_trade", fake_ingest_trade)

    resp = post_json("/extension/trade", {
        "accountId": "1917136",
        "accountType": "2_step_master",
        "accountSize": 10000,
        "symbol": "US30",
        "direction": "buy",
        "volume": 0.2,
        "openPrice": 39000,
        "closePrice": 39050,
        "pnl": 25,
        "closedAt": "2026-04-16T10:00:00Z",
        "source": "scraper",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert captured["connector_type"] == "fundingpips_extension"
    assert captured["source"] == "scraper"


def test_extension_data_compatibility_and_stale_cleanup(monkeypatch):
    calls = {"upsert": 0, "snapshot": 0, "position": 0, "deactivate": []}

    async def fake_upsert(payload):
        calls["upsert"] += 1
        return {"id": 99}

    async def fake_snapshot(payload):
        calls["snapshot"] += 1
        return True

    async def fake_position(payload):
        calls["position"] += 1
        return "US30|buy|na"

    async def fake_deactivate(trading_account_id, seen_position_keys, allow_empty_snapshot=False):
        calls["deactivate"].append({
            "trading_account_id": trading_account_id,
            "seen": seen_position_keys,
            "allow_empty": allow_empty_snapshot,
        })
        return 0

    async def fake_link(*args, **kwargs):
        return None

    monkeypatch.setattr("app.main.upsert_trading_account", fake_upsert)
    monkeypatch.setattr("app.main.ingest_account_snapshot", fake_snapshot)
    monkeypatch.setattr("app.main.ingest_position", fake_position)
    monkeypatch.setattr("app.main.deactivate_missing_positions", fake_deactivate)
    monkeypatch.setattr("app.main.db_link_account", fake_link)

    resp = post_json("/extension/data", {
        "accountId": "1917136",
        "accountType": "2_step_master",
        "accountSize": 10000,
        "balance": 10125,
        "equity": 10120,
        "hasPositions": True,
        "openPositionCount": 1,
        "positions": [{"symbol": "US30", "side": "buy"}],
        "alerts": [],
        "closedTrades": [],
        "telegramUserId": "123",
    })
    assert resp.status_code == 200

    resp2 = post_json("/extension/data", {
        "accountId": "1917136",
        "accountType": "2_step_master",
        "accountSize": 10000,
        "balance": 10125,
        "equity": 10120,
        "hasPositions": False,
        "openPositionCount": 0,
        "positions": [],
        "alerts": [],
        "closedTrades": [],
    })
    assert resp2.status_code == 200

    assert calls["upsert"] == 2
    assert calls["snapshot"] == 2
    assert calls["position"] == 1
    assert calls["deactivate"][0]["allow_empty"] is False
    assert calls["deactivate"][-1]["allow_empty"] is True


def test_account_key_dedup_behavior():
    # Non user-scoped connector: same external account resolves to same key even when user changes.
    k1 = compute_account_key("fundingpips_extension", None, "1917136")
    k2 = compute_account_key("fundingpips_extension", "u-1", "1917136")
    assert k1 == k2

    # User-scoped connector: same external id for different users must be distinct.
    m1 = compute_account_key("manual", "u-1", "journal-default")
    m2 = compute_account_key("manual", "u-2", "journal-default")
    assert m1 != m2
