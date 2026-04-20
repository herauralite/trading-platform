import os
import sys
import types
import asyncio
from datetime import datetime, timedelta, timezone
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


def get_json_auth(path: str, telegram_user_id: str = "123"):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token(telegram_user_id)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, headers={"Authorization": f"Bearer {token}"})

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


def test_account_workspaces_list_route(monkeypatch):
    async def fake_list(uid):
        assert uid == "u-42"
        return [{
            "account_key": "acct-key-1",
            "trading_account_id": 10,
            "user_id": uid,
            "external_account_id": "ext-1",
            "display_label": "Main",
            "broker_name": "FundingPips",
            "broker_family": "fundingpips",
            "connector_type": "fundingpips_extension",
            "connection_status": "connected",
            "sync_state": "idle",
            "account_type": None,
            "account_size": None,
            "last_activity_at": None,
            "last_sync_at": None,
            "is_primary": False,
            "environment": "paper",
            "account_summary": {"equity": 1250.5},
            "last_validated_at": "2026-04-19T00:00:00+00:00",
        }]

    monkeypatch.setattr("app.main.list_account_workspaces", fake_list)
    resp = get_json_auth("/accounts/workspaces", telegram_user_id="u-42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["workspaces"][0]["account_key"] == "acct-key-1"
    assert body["workspaces"][0]["environment"] == "paper"
    assert body["workspaces"][0]["account_summary"]["equity"] == 1250.5


def test_account_workspaces_detail_route_not_found(monkeypatch):
    async def fake_get(uid, account_key):
        assert uid == "u-42"
        assert account_key == "missing"
        return None

    monkeypatch.setattr("app.main.get_account_workspace", fake_get)
    resp = get_json_auth("/accounts/workspaces/missing", telegram_user_id="u-42")
    assert resp.status_code == 404


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


def test_connector_sync_enqueues_run(monkeypatch):
    captured = {}

    async def fake_enqueue(user_id, connector_type, trigger="manual", metadata=None):
        captured["user_id"] = user_id
        captured["connector_type"] = connector_type
        captured["trigger"] = trigger
        return {"id": 55, "status": "queued", "connector_type": connector_type}

    async def fake_lifecycle(user_id, connector_type):
        return {"user_id": user_id, "connector_type": connector_type, "status": "sync_queued"}

    monkeypatch.setattr("app.main.enqueue_connector_sync_run", fake_enqueue)
    monkeypatch.setattr("app.main.get_connector_lifecycle", fake_lifecycle)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-sync")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/connectors/fundingpips_extension/sync", headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "queued"
    assert captured["user_id"] == "u-sync"
    assert captured["connector_type"] == "fundingpips_extension"


def test_connector_sync_rejects_unsupported_connector(monkeypatch):
    called = {"enqueue": 0}

    async def fake_enqueue(*_args, **_kwargs):
        called["enqueue"] += 1
        return {"id": 1}

    monkeypatch.setattr("app.main.enqueue_connector_sync_run", fake_enqueue)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u-sync")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/connectors/manual/sync", headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 409
    assert "does not support remote sync" in resp.json()["detail"]
    assert called["enqueue"] == 0


def test_connector_sync_runs_route_authenticated(monkeypatch):
    async def fake_get_runs(user_id, connector_type, limit=10):
        assert user_id == "u1"
        assert connector_type == "csv_import"
        assert limit == 5
        return [{"id": 1, "status": "succeeded"}]

    monkeypatch.setattr("app.main.get_connector_sync_runs", fake_get_runs)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u1")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/csv_import/sync-runs?limit=5", headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert resp.json()["runs"][0]["status"] == "succeeded"


def test_perform_fundingpips_sync_returns_structured_summary(monkeypatch):
    async def fake_get_config(*_args, **_kwargs):
        return None

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeConn:
        async def execute(self, stmt, _params=None):
            sql = str(stmt)
            if "FROM trading_accounts ta" in sql:
                return FakeResult([{
                    "id": 1,
                    "external_account_id": "fp-1",
                    "display_label": "FundingPips One",
                    "last_snapshot_at": datetime.now(timezone.utc),
                    "open_positions": 2,
                }])
            if "FROM trades" in sql:
                return FakeResult([{"total": 4}])
            if "FROM connector_events" in sql:
                return FakeResult([{"total": 1}])
            raise AssertionError(f"Unexpected SQL: {sql}")

    class FakeConnect:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnect()

    monkeypatch.setattr(ci, "engine", FakeEngine())
    monkeypatch.setattr(ci, "get_connector_config", fake_get_config)
    result = asyncio.run(ci._perform_connector_sync({
        "id": 10,
        "user_id": "u1",
        "connector_type": "fundingpips_extension",
    }))
    assert result["result_category"] == "connector_sync_summary"
    assert result["counts"]["accounts_total"] == 1
    assert result["counts"]["accounts_fresh"] == 1
    assert result["counts"]["trades_24h"] == 4


def test_perform_fundingpips_sync_stale_data_raises_structured_error(monkeypatch):
    async def fake_get_config(*_args, **_kwargs):
        return None

    stale_snapshot = datetime.now(timezone.utc) - timedelta(hours=2)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeConn:
        async def execute(self, stmt, _params=None):
            sql = str(stmt)
            if "FROM trading_accounts ta" in sql:
                return FakeResult([{
                    "id": 1,
                    "external_account_id": "fp-1",
                    "display_label": "FundingPips One",
                    "last_snapshot_at": stale_snapshot,
                    "open_positions": 0,
                }])
            if "FROM trades" in sql:
                return FakeResult([{"total": 0}])
            if "FROM connector_events" in sql:
                return FakeResult([{"total": 0}])
            raise AssertionError(f"Unexpected SQL: {sql}")

    class FakeConnect:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnect()

    monkeypatch.setattr(ci, "engine", FakeEngine())
    monkeypatch.setattr(ci, "get_connector_config", fake_get_config)
    with pytest.raises(ci.ConnectorSyncError) as exc:
        asyncio.run(ci._perform_connector_sync({
            "id": 10,
            "user_id": "u1",
            "connector_type": "fundingpips_extension",
        }))
    assert exc.value.code == "stale_source_data"
    assert exc.value.category == "source_staleness"
    assert exc.value.transient is True


def test_execute_connector_sync_run_retries_then_fails(monkeypatch):
    updates = []
    lifecycle_updates = []

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {
                "id": 42,
                "user_id": "u1",
                "connector_type": "csv_import",
                "status": "running",
                "max_retries": 1,
                "retry_count": 0,
                "lease_owner": "worker-1",
            }

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

    async def fake_set_status(run_id, **kwargs):
        updates.append((run_id, kwargs))
        return {"id": run_id, "status": kwargs["status"]}

    async def fake_lifecycle(**kwargs):
        lifecycle_updates.append(kwargs)
        return kwargs

    async def fake_perform(_run):
        raise RuntimeError("boom")

    monkeypatch.setattr(ci, "engine", FakeEngine())
    monkeypatch.setattr(ci, "_set_sync_run_status", fake_set_status)
    monkeypatch.setattr(ci, "upsert_connector_lifecycle", fake_lifecycle)
    monkeypatch.setattr(ci, "_perform_connector_sync", fake_perform)
    monkeypatch.setattr(ci, "SYNC_RUN_RETRY_DELAYS_SECONDS", [0])

    result = asyncio.run(ci.execute_connector_sync_run(42, worker_id="worker-1"))
    assert result["status"] == "retrying"
    assert [u[1]["status"] for u in updates] == ["running", "retrying"]
    assert updates[1][1]["retry_count"] == 1
    assert updates[1][1]["next_retry_at"] is not None
    assert lifecycle_updates[-1]["status"] == "sync_retrying"


def test_execute_connector_sync_run_non_transient_errors_fail_without_retry(monkeypatch):
    updates = []

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {
                "id": 43,
                "user_id": "u1",
                "connector_type": "fundingpips_extension",
                "status": "running",
                "max_retries": 2,
                "retry_count": 0,
                "lease_owner": "worker-1",
            }

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

    async def fake_set_status(run_id, **kwargs):
        updates.append((run_id, kwargs))
        return {"id": run_id, "status": kwargs["status"], "result_detail": kwargs.get("result_detail")}

    async def fake_lifecycle(**kwargs):
        return kwargs

    async def fake_perform(_run):
        raise ci.ConnectorSyncError(
            "Manual connector cannot execute remote sync",
            code="unsupported_live_sync_connector",
            category="not_supported",
            transient=False,
            status_detail="Unsupported for this connector",
        )

    monkeypatch.setattr(ci, "engine", FakeEngine())
    monkeypatch.setattr(ci, "_set_sync_run_status", fake_set_status)
    monkeypatch.setattr(ci, "upsert_connector_lifecycle", fake_lifecycle)
    monkeypatch.setattr(ci, "_perform_connector_sync", fake_perform)

    result = asyncio.run(ci.execute_connector_sync_run(43, worker_id="worker-1"))
    assert result["status"] == "failed"
    assert [u[1]["status"] for u in updates] == ["running", "failed"]
    assert updates[1][1]["result_detail"]["error_code"] == "unsupported_live_sync_connector"


def test_run_connector_sync_once_claims_and_executes(monkeypatch):
    calls = {"claim": 0, "execute": 0}

    async def fake_claim(worker_id=ci.SYNC_WORKER_ID, lease_seconds=ci.SYNC_RUN_LEASE_SECONDS):
        calls["claim"] += 1
        assert worker_id == "w-1"
        return {"id": 90, "status": "running"}

    async def fake_execute(run_id, worker_id=ci.SYNC_WORKER_ID):
        calls["execute"] += 1
        assert run_id == 90
        assert worker_id == "w-1"
        return {"id": run_id, "status": "succeeded"}

    monkeypatch.setattr(ci, "claim_next_connector_sync_run", fake_claim)
    monkeypatch.setattr(ci, "execute_connector_sync_run", fake_execute)

    result = asyncio.run(ci.run_connector_sync_once(worker_id="w-1"))
    assert result["status"] == "succeeded"
    assert calls == {"claim": 1, "execute": 1}


def test_run_connector_sync_once_noop_without_claim(monkeypatch):
    async def fake_claim(worker_id=ci.SYNC_WORKER_ID, lease_seconds=ci.SYNC_RUN_LEASE_SECONDS):
        return None

    monkeypatch.setattr(ci, "claim_next_connector_sync_run", fake_claim)
    result = asyncio.run(ci.run_connector_sync_once(worker_id="w-1"))
    assert result is None


def test_connector_sync_worker_loop_polls_until_stop(monkeypatch):
    call_count = {"runs": 0}
    stop = asyncio.Event()

    async def fake_run_once(worker_id=ci.SYNC_WORKER_ID):
        call_count["runs"] += 1
        if call_count["runs"] == 1:
            return {"id": 1, "status": "succeeded"}
        stop.set()
        return None

    monkeypatch.setattr(ci, "run_connector_sync_once", fake_run_once)
    asyncio.run(ci.connector_sync_worker_loop(stop, worker_id="w-2", idle_poll_seconds=0.01))
    assert call_count["runs"] >= 2


def test_claim_sql_covers_queued_retrying_and_stale_running(monkeypatch):
    executed_sql = {"text": None, "params": None}

    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {"id": 7, "status": "running"}

    class FakeConn:
        async def execute(self, stmt, params=None):
            executed_sql["text"] = str(stmt)
            executed_sql["params"] = params
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
    claimed = asyncio.run(ci.claim_next_connector_sync_run(worker_id="w-claim", lease_seconds=60))
    assert claimed["id"] == 7
    sql = executed_sql["text"]
    assert "status = 'queued'" in sql
    assert "status = 'retrying'" in sql
    assert "status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= NOW()" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert executed_sql["params"]["worker_id"] == "w-claim"


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


def test_runtime_guard_rejects_multi_process_web(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError) as excinfo:
        main_mod.enforce_single_process_runtime()
    assert "WEB_CONCURRENCY=1" in str(excinfo.value)


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


def test_connector_lifecycle_routes_require_authenticated_session():
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/connectors/manual/sync")

    resp = asyncio.run(_run())
    assert resp.status_code == 401


def test_connector_connect_sync_disconnect_flow(monkeypatch):
    captured = {"upserts": [], "trading_accounts_sql": [], "enqueues": []}

    async def fake_lifecycle(**kwargs):
        captured["upserts"].append(kwargs)
        return {"connector_type": kwargs["connector_type"], "status": kwargs["status"], "is_connected": kwargs["is_connected"]}

    async def fake_account_upsert(payload):
        return {"id": 44, **payload}

    class FakeConn:
        async def execute(self, stmt, params=None):
            captured["trading_accounts_sql"].append((str(stmt), params))
            return None

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(main_mod, "upsert_connector_lifecycle", fake_lifecycle)
    monkeypatch.setattr(main_mod, "upsert_trading_account", fake_account_upsert)
    monkeypatch.setattr("app.core.database.engine", FakeEngine())
    async def fake_enqueue(**kwargs):
        captured["enqueues"].append(kwargs)
        return {"id": 9, "status": "queued"}

    async def fake_get_lifecycle(*_args, **_kwargs):
        return {"status": "sync_queued", "is_connected": True}

    monkeypatch.setattr(main_mod, "enqueue_connector_sync_run", fake_enqueue)
    monkeypatch.setattr(main_mod, "get_connector_lifecycle", fake_get_lifecycle)

    async def _run(path, payload=None):
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("flow-1")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, json=payload or {}, headers={"Authorization": f"Bearer {token}"})

    connect = asyncio.run(_run("/connectors/manual/connect", {"external_account_id": "manual-a"}))
    sync = asyncio.run(_run("/connectors/fundingpips_extension/sync"))
    disconnect = asyncio.run(_run("/connectors/manual/disconnect"))

    assert connect.status_code == 200
    assert sync.status_code == 200
    assert disconnect.status_code == 200
    assert captured["upserts"][0]["status"] == "connected"
    assert captured["upserts"][1]["status"] == "disconnected"
    assert captured["enqueues"][0]["connector_type"] == "fundingpips_extension"
    assert any("UPDATE trading_accounts" in sql for sql, _ in captured["trading_accounts_sql"])


def test_connector_status_detail_uses_lifecycle_fallback(monkeypatch):
    async def fake_overview(_uid):
        return []

    async def fake_lifecycle(_uid, connector_type):
        assert connector_type == "csv_import"
        return {
            "status": "degraded",
            "is_connected": True,
            "last_activity_at": "2026-04-16T12:00:00Z",
            "last_sync_at": "2026-04-16T11:00:00Z",
            "last_error": "sync timeout",
            "last_error_at": "2026-04-16T12:01:00Z",
        }

    monkeypatch.setattr(main_mod, "db_get_connectors_overview", fake_overview)
    monkeypatch.setattr(main_mod, "get_connector_lifecycle", fake_lifecycle)
    async def fake_runs(*_args, **_kwargs):
        return []

    monkeypatch.setattr(main_mod, "get_connector_sync_runs", fake_runs)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("user-55")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/connectors/csv_import", headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    connector = resp.json()["connector"]
    assert connector["status"] == "degraded"
    assert connector["connector_type"] == "csv_import"
    assert connector["account_count"] == 0


def test_connector_config_routes_mask_secrets_and_isolate_owner(monkeypatch):
    stored = {}

    async def fake_upsert(user_id, connector_type, **kwargs):
        key = (user_id, connector_type)
        row = {
            "user_id": user_id,
            "connector_type": connector_type,
            "status": kwargs.get("status", "configured"),
            "non_secret_config": kwargs.get("non_secret_config") or {},
            "secret_config": kwargs.get("secret_config") or {},
            "validation_error": kwargs.get("validation_error"),
            "configured_at": "2026-04-17T10:00:00Z",
            "rotated_at": "2026-04-17T10:00:00Z",
            "created_at": "2026-04-17T10:00:00Z",
            "updated_at": "2026-04-17T10:00:00Z",
        }
        stored[key] = row
        return row

    async def fake_get(user_id, connector_type, include_secret=False):
        row = stored.get((user_id, connector_type))
        if not row:
            return None
        if include_secret:
            return row
        return {
            "user_id": user_id,
            "connector_type": connector_type,
            "status": row["status"],
            "non_secret_config": row["non_secret_config"],
            "has_secret_config": bool(row["secret_config"]),
            "configured_secret_fields": list(row["secret_config"].keys()),
            "validation_error": row["validation_error"],
        }

    async def fake_clear(user_id, connector_type):
        return stored.pop((user_id, connector_type), None) is not None

    monkeypatch.setattr(main_mod, "upsert_connector_config", fake_upsert)
    monkeypatch.setattr(main_mod, "get_connector_config", fake_get)
    monkeypatch.setattr(main_mod, "clear_connector_config", fake_clear)

    async def _run(method, user_id, path, payload=None):
        transport = httpx.ASGITransport(app=app)
        token = create_session_token(user_id)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, json=payload, headers={"Authorization": f"Bearer {token}"})

    save_resp = asyncio.run(_run("PUT", "owner-1", "/connectors/fundingpips_extension/config", {
        "non_secret_config": {
            "healthcheck_url": "https://sync.example.com/health",
            "external_account_id": "acct-a",
        },
        "secret_config": {"api_token": "super-secret-token"},
    }))
    assert save_resp.status_code == 200
    body = save_resp.json()
    assert body["config"]["has_secret_config"] is True
    assert "api_token" in body["config"]["configured_secret_fields"]
    assert "secret_config" not in body["config"]

    owner_can_read = asyncio.run(_run("GET", "owner-1", "/connectors/fundingpips_extension/config"))
    assert owner_can_read.status_code == 200
    assert owner_can_read.json()["has_secret_config"] is True
    assert "api_token" in owner_can_read.json()["configured_secret_fields"]

    other_user = asyncio.run(_run("GET", "owner-2", "/connectors/fundingpips_extension/config"))
    assert other_user.status_code == 200
    assert other_user.json()["status"] == "not_configured"
    assert other_user.json()["has_secret_config"] is False

    cleared = asyncio.run(_run("DELETE", "owner-1", "/connectors/fundingpips_extension/config"))
    assert cleared.status_code == 200
    assert cleared.json()["removed"] is True


def test_sync_execution_uses_configured_external_probe(monkeypatch):
    captured = {"url": None, "headers": None}

    async def fake_get_config(user_id, connector_type, include_secret=False):
        assert user_id == "sync-user"
        assert connector_type == "fundingpips_extension"
        return {
            "status": "configured",
            "non_secret_config": {
                "healthcheck_url": "https://sync.example.com/health",
                "external_account_id": "acct-9",
                "timeout_seconds": 5,
            },
            "secret_config": {"api_token": "token-9"},
        }

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"status": "ok", "message": "healthy"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(ci, "get_connector_config", fake_get_config)
    monkeypatch.setattr(ci.httpx, "AsyncClient", FakeClient)

    run = {"user_id": "sync-user", "connector_type": "fundingpips_extension"}
    result = asyncio.run(ci._perform_fundingpips_sync(run))
    assert result["result_category"] == "external_probe"
    assert captured["url"] == "https://sync.example.com/health"
    assert captured["headers"]["Authorization"] == "Bearer token-9"


def test_connectors_catalog_includes_mt5():
    resp = get_json_auth("/connectors/catalog", telegram_user_id="u1")
    assert resp.status_code == 200
    connectors = resp.json().get("connectors") or []
    mt5 = next((c for c in connectors if c.get("connector_type") == "mt5_bridge"), None)
    assert mt5 is not None
    assert mt5.get("integration_status") == "beta_bridge_required"
    assert mt5.get("connection_layer") == "broker_connector"


def test_mt5_config_validation_requires_bridge_fields(monkeypatch):
    captured = {}

    async def fake_upsert(user_id, connector_type, **kwargs):
        captured["user_id"] = user_id
        captured["connector_type"] = connector_type
        captured["kwargs"] = kwargs
        return {"updated_at": datetime.now(timezone.utc).isoformat()}

    async def fake_get(*_args, **_kwargs):
        return {"status": "incomplete", "non_secret_config": {}, "has_secret_config": False}

    monkeypatch.setattr("app.main.upsert_connector_config", fake_upsert)
    monkeypatch.setattr("app.main.get_connector_config", fake_get)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        token = create_session_token("u2")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.put("/connectors/mt5_bridge/config", json={
                "non_secret_config": {},
                "secret_config": {},
            }, headers={"Authorization": f"Bearer {token}"})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert captured["connector_type"] == "mt5_bridge"
    assert captured["kwargs"]["status"] == "incomplete"
    assert "bridge_url is required" in (captured["kwargs"]["validation_error"] or "")


def test_fundingpips_hydration_backfills_legacy_accounts_and_lifecycle(monkeypatch):
    from app.services import fundingpips_hydration as hydration_mod

    captured_upserts = []
    captured_lifecycle = []

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class FakeConn:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "FROM prop_accounts" in sql:
                return FakeResult([
                    {
                        "account_id": "1917136",
                        "broker": "fundingpips",
                        "account_type": "2_step_master",
                        "account_size": 10000,
                        "label": "Legacy FundingPips",
                        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
                    }
                ])
            if "FROM trading_accounts" in sql:
                return FakeResult([])
            raise AssertionError(f"Unexpected SQL: {sql}")

    class FakeConnect:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConnect()

    async def fake_upsert_trading_account(payload):
        captured_upserts.append(payload)
        return {"id": 77, "external_account_id": payload["external_account_id"]}

    async def fake_get_lifecycle(user_id, connector_type):
        assert user_id == "hydration-user"
        assert connector_type == "fundingpips_extension"
        return None

    async def fake_upsert_lifecycle(**kwargs):
        captured_lifecycle.append(kwargs)
        return kwargs

    monkeypatch.setattr(hydration_mod, "engine", FakeEngine())
    monkeypatch.setattr(hydration_mod, "upsert_trading_account", fake_upsert_trading_account)
    monkeypatch.setattr(hydration_mod, "get_connector_lifecycle", fake_get_lifecycle)
    monkeypatch.setattr(hydration_mod, "upsert_connector_lifecycle", fake_upsert_lifecycle)

    result = asyncio.run(
        hydration_mod.hydrate_fundingpips_canonical_state("hydration-user", trigger="auth_me")
    )

    assert result["legacy_account_count"] == 1
    assert result["created_trading_accounts"] == 1
    assert result["connector_lifecycle_updated"] is True
    assert captured_upserts[0]["connector_type"] == "fundingpips_extension"
    assert captured_upserts[0]["external_account_id"] == "1917136"
    assert captured_lifecycle[0]["status"] == "connected"


def test_auth_me_hydrates_legacy_accounts_before_listing(monkeypatch):
    calls = []

    async def fake_hydrate(uid: str, *, trigger: str):
        calls.append((uid, trigger))
        return {"created_trading_accounts": 1}

    async def fake_accounts(uid):
        assert uid == "123"
        return [{"account_id": "1917136", "source_model": "trading_accounts"}]

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
    monkeypatch.setattr(main_mod, "hydrate_fundingpips_canonical_state", fake_hydrate)
    monkeypatch.setattr(main_mod, "db_get_user_accounts", fake_accounts)

    token = create_session_token("123")

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    response = asyncio.run(_run())
    assert response.status_code == 200
    assert calls == [("123", "auth_me")]
    assert response.json()["accounts"][0]["source_model"] == "trading_accounts"
