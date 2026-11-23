"""Owner-reported regression: unlinking and relinking an account replayed old closed-trade Telegram alerts.

Match-Trader path (community_bot.account_pass): a relink creates a NEW mtapi_connections row, and the
initial history backfill imports the account's past closed positions under that new connection id. The
alert key is 'close:<connection id>:<uid>', so every historical close looks unsent. The required behaviour:
relinking sends no alert for any close that happened before the (new) connection existed, and the next
normal close after the relink sends exactly one alert.

Runs only when TALITRADE_TEST_DATABASE_URL names a disposable database ("test"/"disposable")."""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import database  # noqa: E402
from app.services import community_bot  # noqa: E402
from app.services.matchtrader_api.store import SqlStore  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")


@pytest.fixture
def env(monkeypatch):
    original = database.engine
    db = create_async_engine(URL, poolclass=NullPool)
    monkeypatch.setattr(database, "engine", db)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", db)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "000:test-only")
    sent: list[dict] = []

    async def fake_api(client, method, *, data=None, files=None):
        sent.append({"method": method, **(data or {})})
        return {"ok": True}

    async def no_image(*_a, **_k):
        return None
    monkeypatch.setattr(community_bot, "api", fake_api)
    monkeypatch.setattr(community_bot, "alert_image", no_image)
    return db, sent


def run(db, scenario):
    async def _main():
        try:
            await main.ensure_connector_tables()        # trading_accounts (account_pass reads it)
            await SqlStore(lambda: db).ensure_schema()
            await community_bot.setup()
            await scenario()
        finally:
            await db.dispose()
    asyncio.run(_main())


async def _connect(db, user, host, login, created_at):
    async with db.begin() as conn:
        return await conn.scalar(text("""
            INSERT INTO mtapi_connections (user_id, platform_host, external_login, broker_name, account_currency, created_at)
            VALUES (:u, :h, :l, 'FundingPips', 'USD', :c) RETURNING id"""), {"u": user, "h": host, "l": login, "c": created_at})


async def _import_closes(db, connection_id, closes):
    async with db.begin() as conn:
        for uid, symbol, close_time, net in closes:
            await conn.execute(text("""
                INSERT INTO mtapi_closed_positions (connection_id, uid, position_id, symbol, net_profit, open_price,
                    close_price, close_time, raw)
                VALUES (:c, :uid, :uid, :s, :n, 1.1, 1.2, :t, CAST(:raw AS jsonb))
                ON CONFLICT DO NOTHING"""),
                {"c": connection_id, "uid": uid, "s": symbol, "n": net, "t": close_time, "raw": json.dumps({"uid": uid})})


def _closes_to(sent, user):
    return [m for m in sent if str(m.get("chat_id")) == user and "Trade closed" in (m.get("text") or m.get("caption") or "")]


def _texts(sent, user):
    return [m.get("text") or m.get("caption") or "" for m in sent if str(m.get("chat_id")) == user]


def test_relinking_a_match_trader_account_does_not_replay_old_close_alerts(env):
    db, sent = env
    user = "77" + uuid.uuid4().hex[:8]
    host, login = f"mtr-{uuid.uuid4().hex[:6]}.example.test", "4400" + uuid.uuid4().hex[:4]
    now = datetime.now(timezone.utc)
    history = [("h1", "EURUSD", now - timedelta(days=15), 25), ("h2", "US30", now - timedelta(days=10), -12)]

    async def scenario():
        async with db.begin() as conn:                # alerts switched on a month ago
            await conn.execute(text("INSERT INTO telegram_alert_preferences (user_id, enabled_since) VALUES (:u, :t)"),
                               {"u": user, "t": now - timedelta(days=30)})
        # First link (20 days ago); both closes happened while it was linked and were alerted at the time.
        first = await _connect(db, user, host, login, now - timedelta(days=20))
        await _import_closes(db, first, history)
        await community_bot.account_pass()
        assert len(_closes_to(sent, user)) == 2
        sent.clear()

        # Unlink, then relink the same broker account: new connection row + initial history backfill.
        assert await SqlStore(lambda: db).disconnect(first)
        second = await _connect(db, user, host, login, datetime.now(timezone.utc))
        await _import_closes(db, second, history)
        await community_bot.account_pass()
        await community_bot.account_pass()            # later passes do not send them either
        assert _closes_to(sent, user) == [], _texts(sent, user)

        # The next normal close after the relink is alerted exactly once.
        await _import_closes(db, second, [("n1", "GBPUSD", datetime.now(timezone.utc) + timedelta(seconds=1), 40)])
        await community_bot.account_pass()
        await community_bot.account_pass()
        closes = _closes_to(sent, user)
        assert len(closes) == 1 and "GBPUSD" in (closes[0].get("text") or ""), _texts(sent, user)
    run(db, scenario)
