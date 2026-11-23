"""FundingPips end-to-end acceptance tests against a real PostgreSQL.

Flow under test (the one the browser extension drives in production):
    session login -> POST /auth/extension-token (pairing) -> extension token
    -> POST /extension/data (account snapshot + open positions + realtime closes -> Telegram)
    -> POST /extension/trade (closed-trade history scraper)
    -> GET  /extension/journal, /extension/journal/stats, /extension/status, /accounts/workspaces (dashboard)

Every write goes through the real routes and the app's real SQL, on the schema the app itself creates at
startup. Only Telegram's HTTP API is replaced (messages are recorded). Two users, Alice and Bob.

Runs only when TALITRADE_TEST_DATABASE_URL names a disposable database ("test"/"disposable" in its name):
the app's tables are dropped and recreated for every test. Skipped otherwise.

What this does NOT prove: that the real FundingPips web page still renders the shapes the extension's
scraper expects. That needs an authorized live FundingPips account (see LAUNCH_ACCEPTANCE.md).
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import database, ingest_auth  # noqa: E402
from app.core.auth_session import create_session_token  # noqa: E402
from app.services import community_bot  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")

ALICE, BOB = "5550001", "5550002"
ACCT_A, ACCT_B = "FP-1000001", "FP-2000002"
APP_TABLES = ("account_snapshots", "connector_configs", "connector_events", "connector_lifecycle", "connector_sync_runs",
              "integration_tokens", "mt5_bridge_accounts", "mt5_pairing_tokens", "mt5_trusted_bridges", "positions",
              "prop_accounts", "public_api_beta_connections", "telegram_alert_preferences", "telegram_alert_receipts",
              "trades", "trading_accounts", "tradingview_webhook_connections", "users")


def session(user):
    return {"Authorization": f"Bearer {create_session_token(user)}"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class Ctx:
    def __init__(self):
        self.db = None
        self.http = None
        self.telegram: list[tuple[str, str]] = []

    async def q(self, sql, **params):
        async with self.db.connect() as conn:
            return (await conn.execute(text(sql), params)).mappings().all()

    async def pair(self, user):
        res = await self.http.post("/auth/extension-token", headers=session(user))
        assert res.status_code == 200, res.text
        return res.json()["token"]


@pytest.fixture
def ctx(monkeypatch):
    c = Ctx()
    original = database.engine
    c.db = create_async_engine(URL, poolclass=NullPool)
    # Point every module that captured the engine at import time to the disposable database.
    monkeypatch.setattr(database, "engine", c.db)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", c.db)

    async def fake_send(message, chat_id=None):
        c.telegram.append((str(chat_id), message))
    monkeypatch.setattr(main, "send_telegram", fake_send)

    # Receipt-backed delivery (community_bot.deliver) talks to Telegram through community_bot.api; replace
    # only that HTTP call. `c.telegram_failures` makes the next N sends fail like a Telegram 5xx would.
    c.telegram_failures = 0
    c.telegram_attempts = 0
    c.telegram_delay = 0.0

    async def fake_api(client, method, *, data=None, files=None):
        c.telegram_attempts += 1
        if c.telegram_delay:
            await asyncio.sleep(c.telegram_delay)
        if c.telegram_failures > 0:
            c.telegram_failures -= 1
            raise RuntimeError(f"Telegram {method} returned 502")
        c.telegram.append((str(data["chat_id"]), data.get("text") or data.get("caption") or ""))
        return {"message_id": len(c.telegram)}
    monkeypatch.setattr(community_bot, "api", fake_api)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token-not-real")
    main.account_data_store.clear()
    yield c
    main.account_data_store.clear()


def run(c, scenario):
    async def _main():
        async with c.db.begin() as conn:
            for table in APP_TABLES:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        # The schema exactly as the app creates it at startup (same order as the lifespan).
        await main.ensure_trades_table()
        await main.ensure_users_tables()
        await main.ensure_connector_tables()
        await ingest_auth.ensure_integration_tokens_table()
        await community_bot.setup()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as http:
                c.http = http
                await scenario()
        finally:
            await c.db.dispose()
    asyncio.run(_main())


def heartbeat(account=ACCT_A, **overrides):
    body = {"accountId": account, "accountType": "2_step_phase1", "accountSize": 100000, "accountLabel": "FP 100K",
            "balance": 100250.0, "equity": 100180.0, "profit": -70.0, "hasPositions": True, "openPositionCount": 1,
            "positions": [{"symbol": "US30", "direction": "Buy", "volume": 1.0, "openPrice": 42000.5, "profit": -70.0}],
            "dailyLoss": {"used": 0.0, "remaining": 5000, "limit": 5000}, "overallLoss": {"used": 0.0, "remaining": 10000, "limit": 10000},
            "timestamp": datetime.now(timezone.utc).isoformat()}
    body.update(overrides)
    return body


def trade(account=ACCT_A, **overrides):
    body = {"accountId": account, "accountType": "2_step_phase1", "accountSize": 100000, "symbol": "US30",
            "direction": "Buy", "volume": 1.0, "openPrice": 42000.5, "closePrice": 42050.5, "pnl": 50.0,
            "closedAt": "2026-11-20T14:30:00.000Z", "source": "scraper"}
    body.update(overrides)
    return body


# ── 1 / 2: ownership isolation and extension pairing ─────────────────────────

def test_two_users_are_isolated_end_to_end(ctx):
    async def scenario():
        tok_a, tok_b = await ctx.pair(ALICE), await ctx.pair(BOB)
        assert (await ctx.http.post("/extension/data", headers=bearer(tok_a), json=heartbeat(ACCT_A))).status_code == 200
        assert (await ctx.http.post("/extension/data", headers=bearer(tok_b), json=heartbeat(ACCT_B))).status_code == 200
        assert (await ctx.http.post("/extension/trade", headers=bearer(tok_a), json=trade(ACCT_A))).status_code == 200

        # Bob cannot write into Alice's account, by snapshot or by trade.
        assert (await ctx.http.post("/extension/data", headers=bearer(tok_b), json=heartbeat(ACCT_A, balance=1.0))).status_code == 403
        assert (await ctx.http.post("/extension/trade", headers=bearer(tok_b), json=trade(ACCT_A, pnl=-999.0))).status_code == 403
        # Bob cannot read Alice's history, stats or live status.
        assert (await ctx.http.get("/extension/journal", params={"account_id": ACCT_A}, headers=bearer(tok_b))).status_code in (403, 404)
        assert (await ctx.http.get("/extension/journal/stats", params={"account_id": ACCT_A}, headers=bearer(tok_b))).status_code in (403, 404)
        bob_status = (await ctx.http.get("/extension/status", headers=bearer(tok_b))).json()
        assert set(bob_status["accounts"]) == {ACCT_B}
        bob_journal = (await ctx.http.get("/extension/journal", headers=bearer(tok_b))).json()
        assert bob_journal["trades"] == []

        # Nothing Bob sent reached Alice's rows.
        rows = await ctx.q("SELECT s.balance FROM account_snapshots s JOIN trading_accounts t ON t.id = s.trading_account_id "
                           "WHERE t.external_account_id = :a", a=ACCT_A)
        assert [r["balance"] for r in rows] == [100250.0]
        assert [r["pnl"] for r in await ctx.q("SELECT pnl FROM trades WHERE account_id = :a", a=ACCT_A)] == [50.0]
        owners = await ctx.q("SELECT external_account_id, user_id FROM trading_accounts ORDER BY external_account_id")
        assert [(r["external_account_id"], r["user_id"]) for r in owners] == [(ACCT_A, ALICE), (ACCT_B, BOB)]
    run(ctx, scenario)


def test_extension_token_is_hashed_scoped_and_revocable(ctx):
    async def scenario():
        token = await ctx.pair(ALICE)
        stored = await ctx.q("SELECT token_hash, scope FROM integration_tokens WHERE user_id = :u", u=ALICE)
        assert len(stored) == 1 and stored[0]["scope"] == "extension_ingest"
        assert token not in stored[0]["token_hash"] and len(stored[0]["token_hash"]) == 64
        # Ingest-only: not accepted as a user session.
        assert (await ctx.http.get("/auth/me", headers=bearer(token))).status_code == 401
        assert (await ctx.http.post("/auth/extension-token", headers=bearer(token))).status_code == 401
        assert (await ctx.http.post("/extension/data", headers=bearer(token), json=heartbeat())).status_code == 200
        # Revocation takes effect immediately.
        assert (await ctx.http.delete("/auth/extension-token", headers=session(ALICE))).json()["revoked"] == 1
        assert (await ctx.http.post("/extension/data", headers=bearer(token), json=heartbeat())).status_code == 401
        # Bob revoking his tokens does not touch Alice's.
        new_alice = await ctx.pair(ALICE)
        await ctx.http.delete("/auth/extension-token", headers=session(BOB))
        assert (await ctx.http.post("/extension/data", headers=bearer(new_alice), json=heartbeat())).status_code == 200
    run(ctx, scenario)


# ── 3 / 6: duplicate-free history and reconciliation ─────────────────────────

def test_repeated_history_ingestion_does_not_duplicate(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        history = [trade(), trade(symbol="XAUUSD", direction="Sell", pnl=-35.25, closedAt="2026-11-20T15:00:00.000Z"),
                   trade(pnl=12.5, closedAt="2026-11-21T09:00:00.000Z")]
        for _ in range(3):                                   # the scraper re-sends everything after each page reload
            for row in history:
                assert (await ctx.http.post("/extension/trade", headers=bearer(tok), json=row)).status_code == 200
        rows = await ctx.q("SELECT symbol, pnl FROM trades WHERE account_id = :a ORDER BY closed_at", a=ACCT_A)
        assert [(r["symbol"], r["pnl"]) for r in rows] == [("US30", 50.0), ("XAUUSD", -35.25), ("US30", 12.5)]

        # Dashboard reconciliation: journal and stats agree with the stored rows.
        journal = (await ctx.http.get("/extension/journal", params={"account_id": ACCT_A}, headers=bearer(tok))).json()
        assert len(journal["trades"]) == 3
        assert round(sum(t["pnl"] for t in journal["trades"]), 2) == round(sum(r["pnl"] for r in rows), 2) == 27.25
        stats = (await ctx.http.get("/extension/journal/stats", params={"account_id": ACCT_A}, headers=bearer(tok))).json()
        assert stats["total"] == 3
    run(ctx, scenario)


def test_trade_without_close_time_is_not_duplicated_on_resend(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        for _ in range(3):
            res = await ctx.http.post("/extension/trade", headers=bearer(tok), json=trade(closedAt=None))
            assert res.status_code == 200 and res.json()["persisted"] is True
        rows = await ctx.q("SELECT closed_at FROM trades WHERE account_id = :a", a=ACCT_A)
        assert len(rows) == 1 and rows[0]["closed_at"] is None
    run(ctx, scenario)


# ── 8: unknown stays unknown ─────────────────────────────────────────────────

def test_missing_pnl_is_rejected_not_stored_as_zero(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        res = await ctx.http.post("/extension/trade", headers=bearer(tok), json=trade(pnl=None))
        assert res.status_code == 200 and res.json()["persisted"] is False
        assert await ctx.q("SELECT 1 FROM trades WHERE account_id = :a", a=ACCT_A) == []
        lifecycle = await ctx.q("SELECT last_error FROM connector_lifecycle WHERE user_id = :u AND connector_type = 'fundingpips_extension'", u=ALICE)
        assert lifecycle and "pnl is missing" in (lifecycle[0]["last_error"] or "")
    run(ctx, scenario)


def test_missing_balance_and_equity_stay_null_and_are_kept_separate(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(balance=100250.0, equity=100180.0))
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(
            balance=None, equity=None, timestamp=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
        rows = await ctx.q("SELECT balance, equity FROM account_snapshots ORDER BY snapshot_time")
        assert [(r["balance"], r["equity"]) for r in rows] == [(100250.0, 100180.0), (None, None)]
    run(ctx, scenario)


# ── 4 / 5: open, partially closed and closed positions ───────────────────────

def test_two_same_symbol_positions_survive_and_partial_close_keeps_the_other(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        two = [{"symbol": "US30", "direction": "Buy", "volume": 1.0, "openPrice": 42000.5, "profit": -70.0},
               {"symbol": "US30", "direction": "Buy", "volume": 0.5, "openPrice": 42100.0, "profit": -85.0}]
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(positions=two, openPositionCount=2))
        active = await ctx.q("SELECT size, average_entry FROM positions WHERE is_active ORDER BY size")
        assert [(r["size"], r["average_entry"]) for r in active] == [(0.5, 42100.0), (1.0, 42000.5)]

        # One of them closes: exactly one stays active, with the remaining position's values.
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(positions=[two[0]], openPositionCount=1))
        active = await ctx.q("SELECT size FROM positions WHERE is_active")
        closed = await ctx.q("SELECT closed_at FROM positions WHERE NOT is_active")
        assert [r["size"] for r in active] == [1.0] and len(closed) == 1 and closed[0]["closed_at"] is not None
    run(ctx, scenario)


def test_flat_account_closes_all_positions_but_a_missing_list_does_not(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        # A scrape that found no position rows but says positions exist must not close anything.
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(positions=[], hasPositions=True))
        assert len(await ctx.q("SELECT 1 FROM positions WHERE is_active")) == 1
        # The page says the account is flat: everything is closed.
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(positions=[], hasPositions=False, openPositionCount=0))
        assert await ctx.q("SELECT 1 FROM positions WHERE is_active") == []
    run(ctx, scenario)


# ── 9 / 10: freshness and dashboard ──────────────────────────────────────────

def test_dashboard_workspace_lists_owned_account_with_last_sync_time(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        await ctx.http.post("/extension/trade", headers=bearer(tok), json=trade())
        res = await ctx.http.get("/accounts/workspaces", headers=session(ALICE))
        assert res.status_code == 200, res.text
        body = res.json()
        workspaces = body.get("workspaces") or body.get("accounts") or body
        ids = [w["external_account_id"] for w in workspaces]
        assert ids == [ACCT_A]
        assert workspaces[0]["last_sync_at"] is not None
        bob = await ctx.http.get("/accounts/workspaces", headers=session(BOB))
        assert bob.status_code == 200 and bob.json()["workspaces"] == []
        overview = await ctx.http.get("/connectors/overview", headers=session(ALICE))
        assert overview.status_code == 200, overview.text
    run(ctx, scenario)


async def _age_heartbeat(ctx, account, seconds):
    """Pretend the last heartbeat arrived `seconds` ago, in the database and in this process's cache."""
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with ctx.db.begin() as conn:
        await conn.execute(text("""UPDATE trading_accounts SET metadata = jsonb_set(metadata, '{last_heartbeat,received_at}', to_jsonb(CAST(:t AS TEXT)))
                                   WHERE external_account_id = :a"""), {"t": when.isoformat(), "a": account})
    main.account_data_store.pop(account, None)


def test_stale_account_keeps_its_last_values_and_says_how_old_they_are(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        live = (await ctx.http.get("/extension/status", headers=bearer(tok))).json()
        assert live["accounts"][ACCT_A]["freshness"]["state"] == "live" and live["stale_accounts"] == {}

        await _age_heartbeat(ctx, ACCT_A, main.ACCOUNT_DATA_TTL_SECONDS + 300)
        body = (await ctx.http.get("/extension/status", headers=bearer(tok))).json()
        assert body["accounts"] == {}                       # never presented as live
        stale = body["stale_accounts"][ACCT_A]
        assert stale["balance"] == 100250.0 and stale["equity"] == 100180.0 and stale["positions"][0]["symbol"] == "US30"
        assert stale["freshness"]["state"] == "stale" and stale["freshness"]["age_seconds"] >= main.ACCOUNT_DATA_TTL_SECONDS + 290
        # Bob sees neither the live nor the stale view of Alice's account.
        bob = (await ctx.http.get("/extension/status", headers=bearer(await ctx.pair(BOB)))).json()
        assert bob["accounts"] == {} and bob["stale_accounts"] == {}
        # A new heartbeat makes it live again.
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(balance=100300.0))
        again = (await ctx.http.get("/extension/status", headers=bearer(tok))).json()
        assert again["accounts"][ACCT_A]["balance"] == 100300.0 and again["stale_accounts"] == {}
    run(ctx, scenario)


def test_live_view_survives_a_restart_and_is_shared_with_another_replica(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(url="https://app.fundingpips.com/secret-path"))
        main.account_data_store.clear()                     # process restart / request served by another replica
        body = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        acct = body["accounts"][ACCT_A]
        assert acct["balance"] == 100250.0 and acct["equity"] == 100180.0 and acct["freshness"]["state"] == "live"
        stored = (await ctx.q("SELECT metadata->'last_heartbeat' AS hb FROM trading_accounts WHERE external_account_id = :a", a=ACCT_A))[0]["hb"]
        hb = stored if isinstance(stored, dict) else __import__("json").loads(stored)
        assert "url" not in hb and "telegramUserId" not in hb and "closedTrades" not in hb   # display fields only
        assert "url" not in acct
    run(ctx, scenario)


def test_risk_alert_does_not_fire_again_after_a_restart(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        warn = [{"type": "daily_loss", "level": "warning", "message": "Daily loss at 80%"}]
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(alerts=warn))
        assert len([m for _, m in ctx.telegram if "Daily loss" in m]) == 1
        main.account_data_store.clear()                     # restart
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(alerts=warn))
        assert len([m for _, m in ctx.telegram if "Daily loss" in m]) == 1
        crit = [{"type": "daily_loss", "level": "critical", "message": "Daily loss at 95%"}]
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(alerts=crit))
        assert len([m for _, m in ctx.telegram if "Daily loss" in m]) == 2   # a real escalation still notifies
    run(ctx, scenario)


# ── 11: disconnect / reconnect ───────────────────────────────────────────────

def test_disconnect_then_reconnect_recovers_without_losing_history(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        await ctx.http.post("/extension/trade", headers=bearer(tok), json=trade())
        res = await ctx.http.post("/connectors/fundingpips_extension/disconnect", headers=session(ALICE))
        assert res.status_code == 200, res.text
        assert (await ctx.q("SELECT is_active FROM trading_accounts WHERE external_account_id = :a", a=ACCT_A))[0]["is_active"] is False
        # Bob cannot take over a disconnected account.
        tok_b = await ctx.pair(BOB)
        assert (await ctx.http.post("/extension/data", headers=bearer(tok_b), json=heartbeat())).status_code == 403
        # Alice's extension resumes: the same account row is reactivated, history intact, no duplicate account.
        assert (await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())).status_code == 200
        accounts = await ctx.q("SELECT user_id, is_active FROM trading_accounts WHERE external_account_id = :a", a=ACCT_A)
        assert [(r["user_id"], r["is_active"]) for r in accounts] == [(ALICE, True)]
        assert len(await ctx.q("SELECT 1 FROM trades WHERE account_id = :a", a=ACCT_A)) == 1
    run(ctx, scenario)


# ── 12: Telegram notifications ───────────────────────────────────────────────

def test_closed_trade_alert_is_sent_once_to_the_owner_even_when_the_extension_retries(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        close = {"accountId": ACCT_A, "symbol": "US30", "direction": "Buy", "volume": 1.0, "openPrice": 42000.5,
                 "pnl": 50.0, "balanceAfter": 100300.0, "dailyLossLimit": 5000, "overallLossLimit": 10000}
        payload = heartbeat(positions=[], hasPositions=False, closedTrades=[close])
        for _ in range(3):                  # lost responses / second tab: the same queued close arrives again
            assert (await ctx.http.post("/extension/data", headers=bearer(tok), json=payload)).status_code == 200
        closed_msgs = [(chat, m) for chat, m in ctx.telegram if "Trade Closed" in m]
        assert len(closed_msgs) == 1 and closed_msgs[0][0] == ALICE and "+50.00" in closed_msgs[0][1]
        # A genuinely different close is still notified.
        second = {**close, "pnl": -20.0, "balanceAfter": 100280.0}
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(positions=[], hasPositions=False, closedTrades=[second]))
        assert len([m for _, m in ctx.telegram if "Trade Closed" in m]) == 2
        # Nothing was ever addressed to anyone but the owner.
        assert {chat for chat, _ in ctx.telegram} == {ALICE}
    run(ctx, scenario)


def test_no_telegram_for_rejected_writes(ctx):
    async def scenario():
        tok_a, tok_b = await ctx.pair(ALICE), await ctx.pair(BOB)
        await ctx.http.post("/extension/data", headers=bearer(tok_a), json=heartbeat())
        ctx.telegram.clear()
        close = {"symbol": "US30", "direction": "Buy", "pnl": 999.0, "balanceAfter": 1.0}
        res = await ctx.http.post("/extension/data", headers=bearer(tok_b), json=heartbeat(closedTrades=[close]))
        assert res.status_code == 403 and ctx.telegram == []
        res = await ctx.http.post("/extension/data", json=heartbeat(closedTrades=[close]))
        assert res.status_code == 401 and ctx.telegram == []
    run(ctx, scenario)


CLOSE = {"accountId": ACCT_A, "symbol": "NAS100", "direction": "Sell", "volume": 2.0, "openPrice": 18500.0,
         "pnl": -42.5, "balanceAfter": 100207.5, "dailyLossLimit": 5000, "overallLossLimit": 10000}


async def _receipts(ctx):
    rows = await ctx.q("SELECT event_key FROM telegram_alert_receipts WHERE user_id = :u AND event_key LIKE 'closed_trade:%'", u=ALICE)
    return [r["event_key"] for r in rows]


def test_failed_closed_trade_alert_stays_retryable_then_is_sent_once(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        payload = heartbeat(positions=[], hasPositions=False, closedTrades=[CLOSE])
        ctx.telegram_failures = 1                                   # Telegram rejects the first attempt
        first = await ctx.http.post("/extension/data", headers=bearer(tok), json=payload)
        # Non-2xx: the extension keeps the close queued and resends it on its next poll.
        assert first.status_code == 503 and first.json()["retryable"] is True and first.json()["undelivered_closed_trades"] == 1
        assert ctx.telegram == [] and await _receipts(ctx) == []    # no receipt left behind to suppress the retry
        # The snapshot was still stored on the failed attempt.
        assert len(await ctx.q("SELECT 1 FROM account_snapshots")) == 1

        retry = await ctx.http.post("/extension/data", headers=bearer(tok), json=payload)
        assert retry.status_code == 200
        assert [(chat, "Trade Closed" in m and "-42.50" in m) for chat, m in ctx.telegram] == [(ALICE, True)]
        assert len(await _receipts(ctx)) == 1

        for _ in range(3):                                          # later re-sends: suppressed, still 2xx
            assert (await ctx.http.post("/extension/data", headers=bearer(tok), json=payload)).status_code == 200
        assert len(ctx.telegram) == 1 and ctx.telegram_attempts == 2
    run(ctx, scenario)


def test_concurrent_retries_from_several_processes_send_once(ctx):
    """Each concurrent request runs on its own database connection (NullPool), so PostgreSQL arbitrates the
    receipt exactly as it does between separate web processes sharing the database."""
    async def scenario():
        tok = await ctx.pair(ALICE)
        payload = heartbeat(positions=[], hasPositions=False, closedTrades=[CLOSE])
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())   # account exists
        ctx.telegram_delay = 0.3                                    # keep the first send in flight
        results = await asyncio.gather(*(ctx.http.post("/extension/data", headers=bearer(tok), json=payload) for _ in range(5)))
        assert [r.status_code for r in results] == [200] * 5
        assert len([m for _, m in ctx.telegram if "Trade Closed" in m]) == 1 and len(await _receipts(ctx)) == 1
    run(ctx, scenario)


def test_concurrent_retry_after_a_failed_in_flight_send_still_delivers(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        payload = heartbeat(positions=[], hasPositions=False, closedTrades=[CLOSE])
        ctx.telegram_delay, ctx.telegram_failures = 0.3, 1          # whichever attempt sends first fails
        results = await asyncio.gather(*(ctx.http.post("/extension/data", headers=bearer(tok), json=payload) for _ in range(3)))
        codes = sorted(r.status_code for r in results)
        assert codes.count(503) == 1 and codes.count(200) == 2
        assert len([m for _, m in ctx.telegram if "Trade Closed" in m]) == 1 and len(await _receipts(ctx)) == 1
    run(ctx, scenario)


def test_without_a_bot_token_closed_trades_are_not_blocked(ctx, monkeypatch):
    async def scenario():
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        tok = await ctx.pair(ALICE)
        res = await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(closedTrades=[CLOSE]))
        assert res.status_code == 200 and ctx.telegram_attempts == 0 and await _receipts(ctx) == []
    run(ctx, scenario)


ACCT_A2 = "FP-1000003"


def test_disconnect_removes_the_account_from_status_and_payout_even_with_a_warm_cache(ctx, monkeypatch):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        warm = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        assert ACCT_A in warm["accounts"]                                   # cache is warm and the account is live

        seen = []

        async def capture(acct_id, acct):
            seen.append((acct_id, dict(acct)))
            return {"account": acct_id}
        monkeypatch.setattr(main, "evaluate_payout_eligibility", capture)
        await ctx.http.get("/extension/payout", params={"account_id": ACCT_A}, headers=session(ALICE))
        assert seen[-1][1].get("balance") == 100250.0                      # live inputs used while active

        res = await ctx.http.post("/connectors/fundingpips_extension/disconnect", headers=session(ALICE))
        assert res.status_code == 200
        # The disconnect route does not touch this process's cache (as when another replica handles it).
        assert ACCT_A in main.account_data_store

        body = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        assert ACCT_A not in body["accounts"] and ACCT_A not in body["stale_accounts"]
        assert body == {"accounts": {}, "count": 0, "stale_accounts": {}}
        assert ACCT_A not in main.account_data_store                       # evicted, not merely hidden

        await ctx.http.get("/extension/payout", params={"account_id": ACCT_A}, headers=session(ALICE))
        assert seen[-1] == (ACCT_A, {})                                   # no cached live values for a disconnected account
    run(ctx, scenario)


def test_deactivated_account_is_dropped_but_an_active_stale_account_keeps_its_last_values(ctx):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(ACCT_A))
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat(ACCT_A2, balance=50100.0, equity=50050.0))
        both = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        assert set(both["accounts"]) == {ACCT_A, ACCT_A2}

        res = await ctx.http.post(f"/auth/accounts/{ACCT_A2}/deactivate", headers=session(ALICE))
        assert res.status_code == 200, res.text
        await _age_heartbeat(ctx, ACCT_A, main.ACCOUNT_DATA_TTL_SECONDS + 600)   # A's feed stopped 12 min ago
        assert ACCT_A2 in main.account_data_store                                # A2 still cached here

        body = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        assert body["accounts"] == {}
        assert set(body["stale_accounts"]) == {ACCT_A}                           # deactivated A2 is in neither field
        stale = body["stale_accounts"][ACCT_A]
        assert stale["balance"] == 100250.0 and stale["equity"] == 100180.0 and stale["freshness"]["state"] == "stale"
    run(ctx, scenario)


def test_status_governance_is_per_user(ctx):
    async def scenario():
        tok_a, tok_b = await ctx.pair(ALICE), await ctx.pair(BOB)
        await ctx.http.post("/extension/data", headers=bearer(tok_a), json=heartbeat(ACCT_A))
        await ctx.http.post("/extension/data", headers=bearer(tok_b), json=heartbeat(ACCT_B))
        await ctx.http.post("/connectors/fundingpips_extension/disconnect", headers=session(BOB))
        bob = (await ctx.http.get("/extension/status", headers=session(BOB))).json()
        assert bob == {"accounts": {}, "count": 0, "stale_accounts": {}}
        # Bob's request evicted only Bob's entry; Alice's cached account and view are untouched.
        assert ACCT_A in main.account_data_store and ACCT_B not in main.account_data_store
        alice = (await ctx.http.get("/extension/status", headers=session(ALICE))).json()
        assert set(alice["accounts"]) == {ACCT_A} and alice["stale_accounts"] == {}
    run(ctx, scenario)
