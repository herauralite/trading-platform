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
        assert (await ctx.http.get("/accounts/workspaces", headers=session(BOB))).status_code == 200
    run(ctx, scenario)


def test_live_status_expires_when_heartbeats_stop(ctx, monkeypatch):
    async def scenario():
        tok = await ctx.pair(ALICE)
        await ctx.http.post("/extension/data", headers=bearer(tok), json=heartbeat())
        assert ACCT_A in (await ctx.http.get("/extension/status", headers=bearer(tok))).json()["accounts"]
        stale = (datetime.utcnow() - timedelta(seconds=main.ACCOUNT_DATA_TTL_SECONDS + 5)).isoformat()
        main.account_data_store[ACCT_A]["last_updated"] = stale
        assert (await ctx.http.get("/extension/status", headers=bearer(tok))).json()["accounts"] == {}
        # The durable record keeps the last snapshot time, so the account is still listed with its age.
        last = await ctx.q("SELECT MAX(snapshot_time) AS t FROM account_snapshots")
        assert last[0]["t"] is not None
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
