"""community_bot.api: Bot API failures carry structured, log-safe fields and are classified correctly."""
import asyncio
import inspect
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services import community_bot  # noqa: E402

TOKEN = "987654:SECRET-unit-token"


def call(response):
    async def _run():
        transport = httpx.MockTransport(lambda request: response)
        async with httpx.AsyncClient(transport=transport) as client:
            return await community_bot.api(client, "sendMessage", data={"chat_id": "1", "text": "x"})
    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


@pytest.mark.parametrize("status,body,permanent,error_code,retry_after", [
    (403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}, True, 403, None),
    (403, {"ok": False, "error_code": 403, "description": "Forbidden: user is deactivated"}, True, 403, None),
    (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, True, 400, None),
    (400, {"ok": False, "error_code": 400, "description": "Bad Request: can not parse entities"}, True, 400, None),
    (429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 12}}, False, 429, 12),
    (500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}, False, 500, None),
    (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}, False, 401, None),
    (404, {"ok": False, "error_code": 404, "description": "Not Found"}, False, 404, None),
    (200, {"ok": False, "error_code": 403, "description": "ok false with a 200 status"}, True, 403, None),
])
def test_bot_api_errors_are_structured_and_classified(status, body, permanent, error_code, retry_after):
    with pytest.raises(community_bot.TelegramApiError) as err:
        call(httpx.Response(status, json=body))
    exc = err.value
    assert isinstance(exc, RuntimeError)                                    # existing `except RuntimeError` still works
    assert (exc.permanent, exc.error_code, exc.retry_after, exc.status_code) == (permanent, error_code, retry_after, status)
    assert str(exc) == f"Telegram sendMessage returned {status}"            # historical message, no response text
    rendered = repr(exc) + str(exc) + repr(vars(exc))
    assert TOKEN not in rendered and "SECRET" not in rendered and body["description"] not in rendered


@pytest.mark.parametrize("response", [httpx.Response(502, text="<html>Bad Gateway</html>"),
                                      httpx.Response(200, text="not json")])
def test_non_json_replies_are_transient(response):
    with pytest.raises(community_bot.TelegramApiError) as err:
        call(response)
    assert err.value.permanent is False and err.value.error_code is None


def test_success_returns_result():
    assert call(httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})) == {"message_id": 5}


def test_settling_permanent_failures_is_opt_in():
    """Other deliver() callers did not opt in, so for them every failure still rolls back and raises."""
    assert inspect.signature(community_bot.deliver).parameters["settle_permanent_failures"].default is False


def test_rejection_exception_exposes_only_structured_fields():
    rejected = community_bot.TelegramDeliveryRejected(community_bot.TelegramApiError("sendMessage", 403, 403))
    assert (rejected.error_code, rejected.status_code) == (403, 403)
    assert str(rejected) == "Telegram rejected sendMessage permanently (error_code=403)"
