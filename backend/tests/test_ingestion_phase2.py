import os
import sys
import types
import asyncio
import httpx
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

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
from app import main as main_mod
from app.core.auth_session import create_session_token
from app.core import auth_session as auth_session_mod
from app.services.connector_ingest import compute_account_key
from app.services import connector_ingest as ci


def post_json(path: str, payload: dict):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, json=payload)

    return asyncio.run(_run())


def post_json_auth(path: str, payload: dict, telegram_user_id: str = "123"):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token(telegram_user_id)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, json=payload, headers={"Authorization": f"Bearer {token}"})

    return asyncio.run(_run())


def test_ingest_accounts_route(monkeypatch):
    captured = {}

    async def fake_upsert(payload):
        captured["payload"] = payload
        return {"id": 123, "external_account_id": payload["external_account_id"]}

    monkeypatch.setattr("app.routers.ingest.upsert_trading_account", fake_upsert)

    resp = post_json_auth("/ingest/accounts", {
        "connector_type": "csv_import",
        "external_account_id": "acct-1",
        "broker_name": "csv",
    }, telegram_user_id="u1")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["payload"]["external_account_id"] == "acct-1"
    assert captured["payload"]["user_id"] == "u1"


def test_ingest_trades_route(monkeypatch):
    async def fake_ingest_trade(payload):
        assert payload["symbol"] == "NAS100"
        assert payload["connector_type"] == "manual"
        assert payload["user_id"] == "u1"
        return True

    monkeypatch.setattr("app.routers.ingest.ingest_trade", fake_ingest_trade)

    resp = post_json_auth("/ingest/trades", {
        "connector_type": "manual",
        "external_account_id": "acct-2",
        "symbol": "NAS100",
        "side": "buy",
        "pnl": 12.5,
    }, telegram_user_id="u1")
    assert resp.status_code == 200
    assert resp.json()["persisted"] is True


def test_ingest_routes_reject_unauthenticated_and_explicit_identity():
    unauth = post_json("/ingest/accounts", {
        "connector_type": "manual",
        "external_account_id": "acct-unauth",
    })
    assert unauth.status_code == 401

    explicit = post_json_auth("/ingest/accounts", {
        "connector_type": "manual",
        "external_account_id": "acct-explicit",
        "user_id": "999",
    }, telegram_user_id="123")
    assert explicit.status_code == 400
    assert "Explicit user_id" in explicit.json()["detail"]


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


def test_ensure_connector_tables_fresh_db_ordering(monkeypatch):
    executed_sql = []

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeConn:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            executed_sql.append(sql)
            if "SELECT id, connector_type, user_id, external_account_id" in sql:
                return FakeResult()
            return FakeResult()

    class FakeBegin:
        def __init__(self):
            self.conn = FakeConn()

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(ci, "engine", FakeEngine())
    asyncio.run(ci.ensure_connector_tables())

    joined = "\n".join(executed_sql)
    assert "CREATE TABLE IF NOT EXISTS account_snapshots" in joined
    assert "CREATE TABLE IF NOT EXISTS positions" in joined
    assert "CREATE TABLE IF NOT EXISTS connector_events" in joined

    idx_create_snapshots = joined.index("CREATE TABLE IF NOT EXISTS account_snapshots")
    idx_rewire_snapshots = joined.index("UPDATE account_snapshots s")
    assert idx_create_snapshots < idx_rewire_snapshots

    idx_create_positions = joined.index("CREATE TABLE IF NOT EXISTS positions")
    idx_rewire_positions = joined.index("UPDATE positions p")
    assert idx_create_positions < idx_rewire_positions

    idx_create_events = joined.index("CREATE TABLE IF NOT EXISTS connector_events")
    idx_rewire_events = joined.index("UPDATE connector_events e")
    assert idx_create_events < idx_rewire_events


def test_ensure_connector_tables_drops_legacy_position_uniqueness(monkeypatch):
    executed_sql = []

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeConn:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            executed_sql.append(sql)
            return FakeResult()

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(ci, "engine", FakeEngine())
    asyncio.run(ci.ensure_connector_tables())
    joined = "\n".join(executed_sql)
    assert "DROP CONSTRAINT IF EXISTS positions_trading_account_id_symbol_side_key" in joined
    assert "ALTER TABLE positions ALTER COLUMN position_key SET NOT NULL" in joined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS positions_account_position_key_uq ON positions(trading_account_id, position_key)" in joined
    assert "WHERE position_key IS NOT NULL" not in joined


def test_same_symbol_side_positions_get_distinct_position_keys(monkeypatch):
    seen_params = []

    async def fake_upsert(_payload):
        return {"id": 7}

    class FakeResult:
        rowcount = 1

    class FakeConn:
        async def execute(self, stmt, params=None):
            if params:
                seen_params.append(params)
            return FakeResult()

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(ci, "upsert_trading_account", fake_upsert)
    monkeypatch.setattr(ci, "engine", FakeEngine())

    k1 = asyncio.run(ci.ingest_position({
        "connector_type": "fundingpips_extension",
        "external_account_id": "1917136",
        "symbol": "US30",
        "side": "buy",
        "opened_at": "2026-04-16T10:00:00Z",
    }))
    k2 = asyncio.run(ci.ingest_position({
        "connector_type": "fundingpips_extension",
        "external_account_id": "1917136",
        "symbol": "US30",
        "side": "buy",
        "opened_at": "2026-04-16T10:01:00Z",
    }))
    assert k1 != k2
    assert seen_params[0]["position_key"] != seen_params[1]["position_key"]
    assert seen_params[0]["position_key"] is not None
    assert seen_params[1]["position_key"] is not None


def test_connectors_catalog_route():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/catalog")

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    connector_types = [c["connector_type"] for c in resp.json()["connectors"]]
    assert "fundingpips_extension" in connector_types
    assert "csv_import" in connector_types
    assert "manual" in connector_types


def test_connectors_overview_route(monkeypatch):
    async def fake_overview(user_id):
        assert user_id == "123"
        return [{
            "connector_type": "manual",
            "status": "connected",
            "last_activity_at": "2026-04-16T00:00:00Z",
            "last_sync_at": None,
            "account_count": 1,
            "accounts": [{"id": 1, "external_account_id": "manual-1"}],
        }]

    monkeypatch.setattr("app.main.db_get_connectors_overview", fake_overview)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/overview")

    resp = asyncio.run(_run())
    assert resp.status_code == 401


def test_connectors_overview_authenticated_session(monkeypatch):
    async def fake_overview(user_id):
        assert user_id == "777"
        return [{"connector_type": "manual", "status": "connected", "account_count": 0, "accounts": []}]

    monkeypatch.setattr("app.main.db_get_connectors_overview", fake_overview)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("777")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/overview", headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_auth_me_requires_authenticated_session(monkeypatch):
    async def fake_accounts(uid):
        assert uid == "123"
        return [{"account_id": "acct-1"}]

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {"telegram_user_id": "123", "telegram_username": "alice"}

    class FakeConn:
        async def execute(self, _stmt, _params=None):
            return FakeResult()

    class FakeConnect:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnect()

    monkeypatch.setattr("app.core.database.engine", FakeEngine())
    monkeypatch.setattr(main_mod, "db_get_user_accounts", fake_accounts)

    async def _run(path: str, headers=None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, headers=headers)

    unauth = asyncio.run(_run("/auth/me?telegram_user_id=123"))
    assert unauth.status_code == 401

    token = create_session_token("123")
    auth = asyncio.run(_run("/auth/me", headers={"Authorization": f"Bearer {token}"}))
    assert auth.status_code == 200
    assert auth.json()["user"]["telegram_user_id"] == "123"


def test_authenticated_ingest_manual_routes_use_session_user(monkeypatch):
    captured = {"account": None, "trade": None}

    async def fake_upsert(payload):
        captured["account"] = payload
        return {"id": 1, "external_account_id": payload["external_account_id"]}

    async def fake_ingest(payload):
        captured["trade"] = payload
        return True

    monkeypatch.setattr("app.routers.ingest.upsert_trading_account", fake_upsert)
    monkeypatch.setattr("app.routers.ingest.ingest_trade", fake_ingest)

    account_resp = post_json_auth("/ingest/accounts", {
        "connector_type": "manual",
        "external_account_id": "manual-1",
    }, telegram_user_id="999")
    trade_resp = post_json_auth("/ingest/trades", {
        "connector_type": "manual",
        "external_account_id": "manual-1",
        "symbol": "NAS100",
        "side": "buy",
        "pnl": 5.0,
    }, telegram_user_id="999")

    assert account_resp.status_code == 200
    assert trade_resp.status_code == 200
    assert captured["account"]["user_id"] == "999"
    assert captured["trade"]["user_id"] == "999"


def test_authenticated_csv_import_uses_session_user(monkeypatch):
    captured = {"accounts": [], "trades": []}

    async def fake_upsert(payload):
        captured["accounts"].append(payload)
        return {"id": 1}

    async def fake_ingest(payload):
        captured["trades"].append(payload)
        return True

    monkeypatch.setattr("app.routers.ingest.upsert_trading_account", fake_upsert)
    monkeypatch.setattr("app.routers.ingest.ingest_trade", fake_ingest)

    resp = post_json_auth("/ingest/csv/trades", {
        "connector_type": "csv_import",
        "external_account_id": "csv-1",
        "rows": [{"symbol": "US30", "side": "buy", "pnl": 10}],
    }, telegram_user_id="abc-1")

    assert resp.status_code == 200
    assert captured["accounts"][0]["user_id"] == "abc-1"
    assert captured["trades"][0]["user_id"] == "abc-1"


def test_session_token_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(Exception) as excinfo:
        auth_session_mod.create_session_token("123")
    assert "SECRET_KEY" in str(excinfo.value)
    with pytest.raises(Exception) as decode_exc:
        auth_session_mod.decode_session_token("abc.def")
    assert "SECRET_KEY" in str(decode_exc.value)


def test_retired_bridge_route_returns_gone():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/auth/session/bridge", params={"telegram_username": "@alice"})

    resp = asyncio.run(_run())
    assert resp.status_code == 410
    assert resp.json()["retired"] is True
    assert resp.json()["replacement"] == "/auth/telegram"


def test_db_link_account_mirrors_legacy_to_canonical(monkeypatch):
    captured = {}

    class FakeConn:
        async def execute(self, _stmt, _params=None):
            return None

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    async def fake_upsert(payload):
        captured.update(payload)
        return {"id": 1}

    monkeypatch.setattr("app.core.database.engine", FakeEngine())
    monkeypatch.setattr(main_mod, "upsert_trading_account", fake_upsert)

    asyncio.run(main_mod.db_link_account(
        telegram_user_id="123",
        account_id="1917136",
        account_type="2_step_master",
        account_size=10000,
        label="Primary",
        broker="fundingpips",
    ))

    assert captured["user_id"] == "123"
    assert captured["connector_type"] == "fundingpips_extension"
    assert captured["external_account_id"] == "1917136"
    assert captured["metadata"]["compat_source"] == "prop_accounts"


def test_db_unified_queries_include_legacy_bridge(monkeypatch):
    executed = {"sql": []}

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeConn:
        async def execute(self, stmt, _params=None):
            executed["sql"].append(str(stmt))
            return FakeResult()

    class FakeConnect:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnect()

    monkeypatch.setattr("app.core.database.engine", FakeEngine())
    asyncio.run(main_mod.db_get_user_accounts("123"))
    asyncio.run(main_mod.db_get_connectors_overview("123"))

    joined = "\n".join(executed["sql"])
    assert "canonical_accounts" in joined
    assert "legacy_fallback" in joined
    assert "legacy_only_accounts" in joined



def test_link_account_route_requires_authenticated_session(monkeypatch):
    async def _run(headers=None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/auth/link-account", params={"account_id": "acct-1"}, headers=headers)

    unauth = asyncio.run(_run())
    assert unauth.status_code == 401

    captured = {}

    async def fake_link(uid, account_id, *_args):
        captured["uid"] = uid
        captured["account_id"] = account_id

    async def fake_accounts(uid):
        return [{"account_id": "acct-1", "user_id": uid}]

    monkeypatch.setattr(main_mod, "db_link_account", fake_link)
    monkeypatch.setattr(main_mod, "db_get_user_accounts", fake_accounts)

    token = create_session_token("session-77")
    auth = asyncio.run(_run(headers={"Authorization": f"Bearer {token}"}))
    assert auth.status_code == 200
    assert captured["uid"] == "session-77"
    assert captured["account_id"] == "acct-1"
    assert auth.json()["accounts"][0]["user_id"] == "session-77"


def test_link_account_route_ignores_explicit_identity_params(monkeypatch):
    captured = {}

    async def fake_link(uid, account_id, *_args):
        captured["uid"] = uid
        captured["account_id"] = account_id

    async def fake_accounts(uid):
        return [{"account_id": "acct-legacy", "user_id": uid}]

    monkeypatch.setattr(main_mod, "db_link_account", fake_link)
    monkeypatch.setattr(main_mod, "db_get_user_accounts", fake_accounts)

    async def _run():
        token = create_session_token("session-canonical")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/auth/link-account",
                params={"account_id": "acct-legacy", "telegram_user_id": "explicit-should-be-ignored"},
                headers={"Authorization": f"Bearer {token}"},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert captured["uid"] == "session-canonical"
    assert captured["account_id"] == "acct-legacy"
    assert resp.json()["accounts"][0]["user_id"] == "session-canonical"


def test_retired_link_account_compat_route_returns_gone():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/auth/link-account/compat", params={"telegram_user_id": "123", "account_id": "acct-1"})

    resp = asyncio.run(_run())
    assert resp.status_code == 410
    assert resp.json()["retired"] is True
    assert resp.json()["replacement"] == "/auth/link-account"
