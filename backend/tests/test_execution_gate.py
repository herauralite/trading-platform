"""Read-only MVP: order execution cannot be reached or enabled in production, enforced server-side."""
import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import execution_gate  # noqa: E402

SERVICES = Path(__file__).resolve().parent.parent / "app" / "services"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TALITRADE_ORDER_EXECUTION", "TALITRADE_ENV", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)


def request(app, method, path):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, json={})
    return asyncio.run(_run())


def test_disabled_by_default():
    assert execution_gate.execution_enabled() is False


@pytest.mark.parametrize("env", [{"RAILWAY_ENVIRONMENT_NAME": "production"}, {"TALITRADE_ENV": "production"},
                                 {"RAILWAY_ENVIRONMENT": "staging"}])
def test_flag_cannot_enable_execution_in_production(monkeypatch, env):
    monkeypatch.setenv("TALITRADE_ORDER_EXECUTION", "enabled")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert execution_gate.execution_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "ENABLED ", "enable"])
def test_only_the_exact_flag_value_enables_outside_production(monkeypatch, value):
    monkeypatch.setenv("TALITRADE_ORDER_EXECUTION", value)
    assert execution_gate.execution_enabled() is (value.strip().lower() == "enabled")


@pytest.mark.parametrize("path", ["/orders", "/order", "/api/v1/orders/123", "/connectors/x/place-order",
                                  "/trade/execute", "/positions/close_position", "/v1/market-orders", "/ORDERS/"])
def test_execution_like_paths_are_blocked_with_403(path):
    for method in ("POST", "GET", "DELETE", "PUT"):
        response = request(main.app, method, path)
        assert response.status_code == 403, (method, path)
        assert "read-only" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/health", "/extension/journal", "/connectors/catalog"])
def test_non_execution_paths_are_not_affected(path):
    assert request(main.app, "GET", path).status_code != 403


def test_no_registered_route_is_an_execution_route():
    offending = [r.path for r in main.app.routes if execution_gate.is_execution_path(getattr(r, "path", ""))]
    assert offending == [], f"order-execution routes must not exist in the read-only MVP: {offending}"


def test_existing_read_only_routes_do_not_trip_the_pattern():
    for path in ("/ingest/trades", "/ingest/positions", "/extension/trade", "/journal/trade",
                 "/connectors/matchtrader-api/connections/{connection_id}/trades", "/admin/dedup-trades"):
        assert not execution_gate.is_execution_path(path), path


def test_route_dependency_blocks_even_without_middleware(monkeypatch):
    app = FastAPI()

    @app.post("/demo/execute-like", dependencies=[Depends(execution_gate.require_execution_enabled)])
    async def _endpoint():
        return {"sent": True}

    assert request(app, "POST", "/demo/execute-like").status_code == 403
    monkeypatch.setenv("TALITRADE_ORDER_EXECUTION", "enabled")
    assert request(app, "POST", "/demo/execute-like").status_code == 200
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert request(app, "POST", "/demo/execute-like").status_code == 403


def test_broker_clients_never_send_trading_requests():
    """Source guard: broker clients may POST only to authentication endpoints; everything else is GET."""
    tradelocker = (SERVICES / "tradelocker_provider.py").read_text(encoding="utf-8")
    posts = re.findall(r'method="(POST|PUT|PATCH|DELETE)",\s*path="([^"]+)"', tradelocker)
    assert {p for _, p in posts} <= {"/auth/jwt/token", "/auth/jwt/refresh"}, posts
    matchtrader = (SERVICES / "matchtrader_api" / "client.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(post|put|patch|delete)\(", matchtrader)
    for name in ("alpaca_provider.py", "mt5_bridge.py", "fundingpips_connector.py", "fundingpips_hydration.py"):
        source = (SERVICES / name).read_text(encoding="utf-8")
        assert not re.search(r"/v2/orders|/orders['\"/]|place_order|submit_order", source), name
