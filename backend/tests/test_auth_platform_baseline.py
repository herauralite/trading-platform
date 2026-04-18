import asyncio
import os
import sys

import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.main import app


def test_auth_telegram_config_exposes_expected_bootstrap_shape():
    async def _run():
      transport = httpx.ASGITransport(app=app)
      async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
          return await client.get("/auth/telegram/config")

    response = asyncio.run(_run())
    assert response.status_code == 200
    body = response.json()

    assert body["authMode"] in {"legacy_widget", "widget", "oidc"}
    assert isinstance(body["botUsername"], str)
    assert isinstance(body["canonicalBotUsername"], str)
    assert isinstance(body["loginDomain"], str)
    assert isinstance(body["canonicalLoginDomain"], str)
    assert isinstance(body["hasBotToken"], bool)
    assert response.headers["x-tali-auth-mode"] == body["authMode"]
    assert response.headers["x-tali-config-version"] == "2026-04-17"


def test_auth_me_requires_authenticated_session_token():
    async def _run():
      transport = httpx.ASGITransport(app=app)
      async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
          return await client.get("/auth/me")

    response = asyncio.run(_run())
    assert response.status_code == 401
    assert "detail" in response.json()
