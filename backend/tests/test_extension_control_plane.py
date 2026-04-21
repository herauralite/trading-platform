import os
from contextlib import asynccontextmanager

import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/testdb")
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.routers import extension_control  # noqa: E402
from app.services import extension_control_plane as service  # noqa: E402
from app.services.execution_status import validate_command_transition  # noqa: E402


class _MappingsResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


class _FakeConn:
    def __init__(self):
        self.sql = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sql.append(sql)
        if "FROM extension_pairing_tokens" in sql:
            return _MappingsResult(
                {
                    "id": 7,
                    "user_id": "u-1",
                    "status": "pending",
                    "expires_at": service._utcnow() + service.timedelta(minutes=5),
                    "pair_secret_hash": service._hash_pair_secret("pair-secret"),
                }
            )
        if "INSERT INTO extension_devices" in sql:
            return _MappingsResult({"id": 9, "user_id": "u-1"})
        if "INSERT INTO extension_sessions" in sql:
            return _MappingsResult({"id": 12})
        return _MappingsResult({"id": 1})


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def begin(self):
        yield self.conn


def test_pairing_completion_returns_extension_auth_material(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(service, "engine", _FakeEngine(conn))

    out = asyncio.run(service.complete_pairing(
        "pair-code",
        "pair-secret",
        {"device_fingerprint": "dev-1", "label": "Laptop"},
    ))

    assert out["extension_device_id"] == 9
    assert out["extension_session_id"] == 12
    assert out["extension_access_token"]
    assert out["token_type"] == "bearer"


def test_state_sync_populates_linkage_fields(monkeypatch):
    captured = {}

    async def fake_upsert(account):
        captured.update(account)
        return {"id": 33, "external_account_id": account["external_account_id"], **account}

    async def fake_snapshot(_payload):
        return True

    async def fake_position(_payload):
        return "pos-1"

    async def fake_deactivate(_aid, _keys, allow_empty_snapshot=False):
        return 0

    async def fake_resolve(*_args, **_kwargs):
        return 88

    class _NoopEngine:
        @asynccontextmanager
        async def begin(self):
            class _Conn:
                async def execute(self, *_args, **_kwargs):
                    return _MappingsResult(None)

            yield _Conn()

    monkeypatch.setattr(service, "upsert_trading_account", fake_upsert)
    monkeypatch.setattr(service, "ingest_account_snapshot", fake_snapshot)
    monkeypatch.setattr(service, "ingest_position", fake_position)
    monkeypatch.setattr(service, "deactivate_missing_positions", fake_deactivate)
    monkeypatch.setattr(service, "_resolve_platform_session_id", fake_resolve)
    monkeypatch.setattr(service, "engine", _NoopEngine())

    asyncio.run(service.ingest_state_sync(
        {"user_id": "u-1", "extension_device_id": 9, "extension_session_id": 12},
        {
            "accounts": [
                {
                    "adapter_key": "fundingpips_browser",
                    "platform_key": "fundingpips",
                    "platform_account_ref": "acct-1",
                    "snapshot": {},
                    "positions": [],
                    "orders": [],
                }
            ]
        },
    ))

    assert captured["platform_key"] == "fundingpips"
    assert captured["platform_account_ref"] == "acct-1"
    assert captured["extension_device_id"] == 9
    assert captured["platform_session_id"] == 88
    assert captured["execution_enabled"] is True


def test_poll_reclaims_stale_dispatched_commands(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(service, "engine", _FakeEngine(conn))

    asyncio.run(service.poll_execution_commands({"user_id": "u-1", "extension_device_id": 9, "extension_session_id": 12}))

    sql_blob = "\n".join(conn.sql)
    assert "status = 'queued'" in sql_blob
    assert "dispatch_lease_expires_at < NOW()" in sql_blob
    assert "dispatch_lease_owner" in sql_blob


def test_extension_authenticated_route_rejects_invalid_token():
    app = FastAPI()
    app.include_router(extension_control.router)
    client = TestClient(app)

    response = client.post("/extension/heartbeat", headers={"Authorization": "Bearer not-a-valid-token"}, json={})
    assert response.status_code == 401


def test_command_transition_graph_accepts_expected_paths():
    assert validate_command_transition("queued", "dispatched")
    assert validate_command_transition("dispatched", "acked")
    assert validate_command_transition("acked", "running")
    assert validate_command_transition("running", "succeeded")


def test_command_transition_graph_rejects_invalid_paths():
    assert not validate_command_transition("queued", "succeeded")
    assert not validate_command_transition("failed", "running")
