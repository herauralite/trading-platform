"""P0 security regression tests: /admin/* and /test/* must not be reachable without
explicit maintenance mode AND the server-side admin token."""
import asyncio
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import admin_auth  # noqa: E402
from app.core.auth_session import create_session_token  # noqa: E402
import app.main as main_mod  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_TOKEN = "unit-test-admin-token-0123456789abcdef-XYZ"
ADMIN = {"X-TaliTrade-Admin-Token": ADMIN_TOKEN}
ADMIN_ROUTES = [
    ("GET", "/admin/dedup-trades/preview"),
    ("DELETE", "/admin/dedup-trades"),
    ("DELETE", "/admin/purge-corrupt-trades"),
    ("GET", "/test/db"),
]
DESTRUCTIVE_ROUTES = [r for r in ADMIN_ROUTES if r[0] == "DELETE"]
SECRET_ROW = {"id": 1, "account_id": "ACCT-SECRET-777", "symbol": "XAUUSD", "pnl": 12.5}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("RAILWAY_", "VERCEL_", "RENDER_", "HEROKU_", "FLY_", "TALITRADE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")


@pytest.fixture
def db(monkeypatch):
    """Fake engine that records every statement, so 'no database access' is provable."""
    state = {"statements": [], "commits": 0, "rollbacks": 0, "fail": False}

    class Result:
        def scalar(self):
            return 4242

        def mappings(self):
            return self

        def all(self):
            return [dict(SECRET_ROW)]

        def first(self):
            return dict(SECRET_ROW)

    class Conn:
        async def execute(self, stmt, params=None):
            state["statements"].append(str(stmt))
            if state["fail"]:
                raise RuntimeError('relation "trades" does not exist at db-prod.internal:5432 password=hunter2')
            return Result()

    class Ctx:
        def __init__(self, transactional):
            self.transactional = transactional

        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, exc_type, exc, tb):
            if self.transactional:
                state["rollbacks" if exc_type else "commits"] += 1
            return False

    class Engine:
        def connect(self):
            return Ctx(False)

        def begin(self):
            return Ctx(True)

    monkeypatch.setattr("app.core.database.engine", Engine())
    monkeypatch.setattr(main_mod, "engine", Engine())
    return state


@pytest.fixture
def maintenance(monkeypatch):
    monkeypatch.setenv("TALITRADE_ADMIN_MAINTENANCE_MODE", "true")
    monkeypatch.setenv("TALITRADE_ADMIN_TOKEN", ADMIN_TOKEN)


def call(method, path, headers=None, client=("203.0.113.9", 4000)):
    async def _run():
        transport = httpx.ASGITransport(app=app, client=client, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as http:
            return await http.request(method, path, headers=headers)

    return asyncio.run(_run())


def assert_denied(res, db, allowed=(401, 403, 404)):
    assert res.status_code in allowed
    assert db["statements"] == [], "a denied request must not touch the database"
    assert db["commits"] == 0
    for leaked in ("ACCT-SECRET-777", "4242", "trades_in_db", "deleted_rows", "total_realtime_rows", ADMIN_TOKEN):
        assert leaked not in res.text


# 1, 2, 5 ── default state: maintenance off, routes behave as if absent ──────────
@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_anonymous_callers_are_refused_by_default(db, method, path):
    assert admin_auth.maintenance_enabled() is False
    res = call(method, path)
    assert res.status_code == 404
    assert_denied(res, db)


@pytest.mark.parametrize("method,path", DESTRUCTIVE_ROUTES)
def test_anonymous_callers_cannot_delete_trades(db, maintenance, method, path):
    res = call(method, path)
    assert res.status_code == 401
    assert_denied(res, db)


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_token_is_useless_while_maintenance_mode_is_off(db, monkeypatch, method, path):
    monkeypatch.setenv("TALITRADE_ADMIN_TOKEN", ADMIN_TOKEN)  # token configured, flag absent
    assert_denied(call(method, path, headers=ADMIN), db, allowed=(404,))
    monkeypatch.setenv("TALITRADE_ADMIN_MAINTENANCE_MODE", "false")
    assert_denied(call(method, path, headers=ADMIN), db, allowed=(404,))


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_maintenance_mode_without_a_strong_token_stays_closed(db, monkeypatch, method, path):
    monkeypatch.setenv("TALITRADE_ADMIN_MAINTENANCE_MODE", "true")
    assert_denied(call(method, path, headers={"X-TaliTrade-Admin-Token": ""}), db, allowed=(404,))
    monkeypatch.setenv("TALITRADE_ADMIN_TOKEN", "short")  # weak token is treated as unset
    assert_denied(call(method, path, headers={"X-TaliTrade-Admin-Token": "short"}), db, allowed=(404,))


# 3 ── ordinary authenticated users ──────────────────────────────────────────────
@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_ordinary_authenticated_users_are_refused(db, maintenance, method, path, monkeypatch):
    session = {"Authorization": f"Bearer {create_session_token('123456789')}"}
    assert_denied(call(method, path, headers=session), db, allowed=(401,))
    # A Telegram user id is never an admin credential, wherever it is put.
    monkeypatch.setenv("PRIMARY_ACCOUNT_ID", "123456789")
    assert_denied(call(method, f"{path}?telegram_user_id=123456789&admin=1", headers=session), db, allowed=(401,))
    assert_denied(call(method, path, headers={**session, "X-TaliTrade-Admin-Token": "123456789"}), db, allowed=(403,))
    # ...and the session token itself is not accepted as the admin token.
    assert_denied(call(method, path, headers={"X-TaliTrade-Admin-Token": session["Authorization"][7:]}), db, allowed=(403,))


# 6 ── invalid / missing / misplaced credentials ────────────────────────────────
@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_invalid_or_missing_admin_credentials_are_rejected(db, maintenance, method, path):
    assert_denied(call(method, path), db, allowed=(401,))
    for wrong in ("wrong", ADMIN_TOKEN[:-1], ADMIN_TOKEN + "x", ADMIN_TOKEN.upper(), "Bearer " + ADMIN_TOKEN):
        assert_denied(call(method, path, headers={"X-TaliTrade-Admin-Token": wrong}), db, allowed=(403,))
    # The token is only honoured in its dedicated header: not the query string, not as a bearer session, not a cookie.
    for query in ("admin_token", "token", "x-talitrade-admin-token", "X-TaliTrade-Admin-Token", "key"):
        assert_denied(call(method, f"{path}?{query}={ADMIN_TOKEN}"), db, allowed=(401,))
    assert_denied(call(method, path, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}), db, allowed=(401,))
    assert_denied(call(method, path, headers={"Cookie": f"x-talitrade-admin-token={ADMIN_TOKEN}"}), db, allowed=(401,))


# 7 ── /test/db ─────────────────────────────────────────────────────────────────
def test_test_db_does_not_leak_database_information(db, maintenance):
    for headers in (None, {"Authorization": f"Bearer {create_session_token('123456789')}"}, {"X-TaliTrade-Admin-Token": "nope"}):
        res = call("GET", "/test/db", headers=headers)
        assert_denied(res, db)
        assert set(res.json()) == {"detail"}
    db["fail"] = True  # even for an admin, a database error must not surface its details
    res = call("GET", "/test/db", headers=ADMIN)
    assert res.status_code == 500
    for leaked in ("hunter2", "db-prod.internal", "relation", "trades"):
        assert leaked not in res.text


# authorized path + defence in depth ────────────────────────────────────────────
def test_authorized_admin_can_run_maintenance_inside_a_transaction(db, maintenance, caplog):
    with caplog.at_level("DEBUG"):
        assert call("GET", "/admin/dedup-trades/preview", headers=ADMIN).status_code == 200
        assert call("GET", "/test/db", headers=ADMIN).json() == {"status": "ok", "trades_in_db": 4242}
        assert db["commits"] == 0  # reads never open a write transaction
        res = call("DELETE", "/admin/dedup-trades", headers=ADMIN)
        assert res.status_code == 200 and res.json()["deleted_count"] == 1
        assert call("DELETE", "/admin/purge-corrupt-trades", headers=ADMIN).status_code == 200
    assert db["commits"] == 2 and db["rollbacks"] == 0
    assert "op=dedup-trades outcome=ok deleted=1" in caplog.text
    assert "op=purge-corrupt-trades outcome=ok deleted=1" in caplog.text
    assert ADMIN_TOKEN not in caplog.text  # credentials are never logged


@pytest.mark.parametrize("method,path", DESTRUCTIVE_ROUTES)
def test_failed_destructive_operation_rolls_back_and_hides_database_details(db, maintenance, caplog, method, path):
    db["fail"] = True
    with caplog.at_level("DEBUG"):
        res = call(method, path, headers=ADMIN)
    assert res.status_code == 500
    assert res.json() == {"detail": "Maintenance operation failed; no changes were made"}
    assert db["commits"] == 0 and db["rollbacks"] == 1
    assert "outcome=failed" in caplog.text
    for leaked in ("hunter2", "db-prod.internal"):
        assert leaked not in res.text and leaked not in caplog.text


def test_denied_attempts_are_logged_without_credentials(db, maintenance, caplog):
    with caplog.at_level("DEBUG"):
        call("DELETE", "/admin/dedup-trades", headers={"X-TaliTrade-Admin-Token": "guess-" + "z" * 40})
    assert "admin denied: DELETE /admin/dedup-trades reason=invalid_credentials" in caplog.text
    assert "z" * 40 not in caplog.text


def test_admin_routes_are_not_advertised_in_the_api_schema():
    paths = app.openapi()["paths"]
    assert not [p for p in paths if p.startswith(("/admin", "/test"))]


def test_every_admin_and_test_route_requires_the_admin_dependency():
    """Guards against a future /admin or /test route being added without protection."""
    guarded = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(("/admin", "/test")):
            deps = [d.call for d in route.dependant.dependencies]
            assert admin_auth.require_admin in deps, f"{path} is missing require_admin"
            guarded.append(path)
    assert sorted(guarded) == sorted(p for _, p in ADMIN_ROUTES)


# 8 ── legitimate endpoints keep working ────────────────────────────────────────
def test_public_and_user_endpoints_are_unaffected(db, monkeypatch):
    assert call("GET", "/health").json() == {"status": "ok"}
    assert call("GET", "/health/db").status_code == 200
    assert call("GET", "/auth/telegram/config").status_code == 200
    assert call("GET", "/connectors/catalog").status_code == 200
    assert call("GET", "/accounts/workspaces").status_code == 401  # still session-protected

    async def fake_overview(uid):
        assert uid == "123456789"
        return []

    monkeypatch.setattr(main_mod, "db_get_connectors_overview", fake_overview)
    session = {"Authorization": f"Bearer {create_session_token('123456789')}"}
    res = call("GET", "/connectors/overview", headers=session)
    assert res.status_code == 200 and res.json() == {"connectors": [], "count": 0}
