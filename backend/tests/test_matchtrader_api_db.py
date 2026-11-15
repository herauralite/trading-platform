"""Match-Trader API integration: store, sync engine and routes against a real PostgreSQL.

These tests DROP and recreate the mtapi_* tables (and a throwaway ``trades`` table), so they
run only when TALITRADE_TEST_DATABASE_URL points at a disposable database whose name contains
"test" or "disposable". They are skipped otherwise. The broker is a scripted fake; no network.
"""
import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import ingest_auth  # noqa: E402
from app.core.auth_session import create_session_token  # noqa: E402
from app.main import app  # noqa: E402
from app.services.matchtrader_api import client as api, config, crypto, engine as sync_engine, ratelimit, read_models, routes, worker  # noqa: E402
from app.services.matchtrader_api.store import DROP_ORDER, IdentityConflict, SqlStore, StaleWriter, now_utc  # noqa: E402
from tests.mt_fakes import TOKEN_A, TOKEN_B, FakeBroker, closed, configure_keys, position  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")

ALICE, BOB = "1001", "2002"
P = "/connectors/matchtrader-api/connections"
NOW = 1_790_000_000_000   # fixed "now" for engine passes (2026-09-21 UTC); always in the past


def auth(user):
    return {"Authorization": f"Bearer {create_session_token(user)}"}


@pytest.fixture
def ctx(monkeypatch):
    configure_keys(monkeypatch)
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "true")
    monkeypatch.setenv("TALITRADE_MT_API_GLOBAL_PER_MINUTE", "1000")   # tests that exercise the account cap set their own
    ratelimit._budgets.clear()
    routes._CONNECT_ATTEMPTS.clear()
    c = SimpleNamespace(broker=FakeBroker(), owners={}, store=None, db=None, http=None)
    c.factory = lambda token, budget=None: api.MatchTraderApiClient(token, budget=budget, transport=c.broker.transport())

    async def owners(account_id):
        return set(c.owners.get(account_id, set()))

    monkeypatch.setattr(ingest_auth, "get_account_owners", owners)
    monkeypatch.setattr(routes, "get_store", lambda: c.store)
    monkeypatch.setattr(routes, "get_read_models", lambda: read_models.ReadModels(lambda: c.db))
    monkeypatch.setattr(routes, "make_client", c.factory)
    return c


def run_db(c, scenario):
    async def main():
        c.db = create_async_engine(URL, poolclass=NullPool)
        async with c.db.begin() as conn:
            for table in DROP_ORDER:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            await conn.execute(text("DROP TABLE IF EXISTS trades"))
            await conn.execute(text("CREATE TABLE trades (id SERIAL PRIMARY KEY, account_id TEXT, symbol TEXT, direction TEXT, volume DOUBLE PRECISION, close_price DOUBLE PRECISION, pnl DOUBLE PRECISION, closed_at TIMESTAMPTZ, source TEXT)"))
        c.store = SqlStore(lambda: c.db)
        await c.store.ensure_schema()
        await c.store.ensure_schema()  # idempotent
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
                c.http = http
                await scenario()
        finally:
            await c.db.dispose()

    asyncio.run(main())


async def q(c, sql, **params):
    async with c.db.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def connect(c, user=ALICE, token=TOKEN_A):
    return await c.http.post(P, headers=auth(user), json={"token": token})


async def sync(c, cid, now=NOW, **kw):
    return await sync_engine.sync_connection(c.store, cid, client_factory=c.factory, now_ms=lambda: now, **kw)


# ── connect, masking, encryption ─────────────────────────────────────────────
def test_connect_binds_verified_identity_and_never_exposes_the_token(ctx, caplog):
    async def scenario():
        res = await connect(ctx)
        assert res.status_code == 200, res.text
        body = res.json()["connection"]
        assert body["status"] == "active" and body["login"] == "700001" and body["platform_host"] == "mtr.broker-one.example"
        assert body["ingestion_mode"] == "shadow" and body["token_hint"] == "...a1b2" and body["linked_account_id"] is None
        assert TOKEN_A not in res.text and "ciphertext" not in res.text and "generation" not in res.text and "email" not in res.text
        listing = await ctx.http.get(P, headers=auth(ALICE))
        assert TOKEN_A not in listing.text and len(listing.json()["connections"]) == 1
        (ciphertext, key_version), = await q(ctx, "SELECT ciphertext, key_version FROM mtapi_credentials")
        assert TOKEN_A not in ciphertext and key_version == "v1"
        dump = str(await q(ctx, "SELECT * FROM mtapi_connections")) + str(await q(ctx, "SELECT * FROM mtapi_credentials"))
        assert TOKEN_A not in dump and "trader@example.com" not in dump   # no raw token, no profile PII
        aad = crypto.build_associated_data(user_id=ALICE, connection_id=body["id"], platform_host="mtr.broker-one.example", external_login="700001")
        assert crypto.decrypt_token(ciphertext, aad) == TOKEN_A
        with pytest.raises(crypto.TokenCryptoError):
            crypto.decrypt_token(ciphertext, crypto.build_associated_data(user_id=BOB, connection_id=body["id"], platform_host="mtr.broker-one.example", external_login="700001"))
        assert {r[0] for r in ctx.broker.requests} == {"GET"}   # validation never probes with a mutation

    with caplog.at_level("DEBUG"):
        run_db(ctx, scenario)
    assert TOKEN_A not in caplog.text


def test_connect_rejects_bad_tokens_and_reports_missing_capabilities(ctx):
    async def scenario():
        res = await connect(ctx, token="unknown-token-" + "z" * 30)
        assert res.status_code == 422 and "z" * 30 not in res.text
        ctx.broker.fail["/v1/closed-positions"] = (403, {})
        res = await connect(ctx)
        body = res.json()["connection"]
        assert body["status"] == "forbidden" and body["status_detail"] == "missing_capabilities:closed_positions"
        assert body["capabilities"]["closed_positions"] is False and body["capabilities"]["positions"] is True
        before = len(ctx.broker.requests)
        assert (await ctx.http.post(f"{P}/{body['id']}/sync", headers=auth(ALICE))).status_code == 409
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0
        assert len(ctx.broker.requests) == before   # a non-active connection is never polled

    run_db(ctx, scenario)


def test_unverifiable_platform_identity_becomes_pending_review_and_is_not_synced(ctx):
    async def scenario():
        ctx.broker.accounts[TOKEN_A]["details"]["platformUrl"] = None
        ctx.owners["700001"] = {ALICE}
        body = (await connect(ctx)).json()["connection"]
        assert body["status"] == "pending_review" and body["status_detail"] == "platform_identity_unverified" and body["linked_account_id"] is None
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0

    run_db(ctx, scenario)


def test_connect_is_throttled_per_user(ctx):
    async def scenario():
        codes = [(await connect(ctx, token="bad-token-" + "y" * 30)).status_code for _ in range(7)]
        assert codes == [422] * 5 + [429] * 2
        assert (await connect(ctx, user=BOB)).status_code == 200   # another user is unaffected

    run_db(ctx, scenario)


# ── ownership and identity ───────────────────────────────────────────────────
def test_other_user_cannot_see_or_control_a_connection(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        bob = auth(BOB)
        assert (await ctx.http.get(P, headers=bob)).json()["connections"] == []
        attempts = [
            ("POST", f"{P}/{cid}/rotate", {"token": TOKEN_B}), ("POST", f"{P}/{cid}/pause", None), ("POST", f"{P}/{cid}/resume", None),
            ("POST", f"{P}/{cid}/sync", None), ("POST", f"{P}/{cid}/backfill", {"from_ms": 1}), ("GET", f"{P}/{cid}/reconciliation", None),
            ("DELETE", f"{P}/{cid}", None), ("DELETE", f"{P}/999999", None),
        ]
        before = len(ctx.broker.requests)
        for method, path, body in attempts:
            res = await ctx.http.request(method, path, headers=bob, json=body)
            assert res.status_code == 404 and res.json() == {"detail": "Connection not found"}, path   # same answer for missing ids
        assert len(ctx.broker.requests) == before
        (status, user, generation), = await q(ctx, "SELECT status, user_id, generation FROM mtapi_connections WHERE id = :id", id=cid)
        assert (status, user, generation) == ("active", ALICE, 1)
        assert len(await q(ctx, "SELECT 1 FROM mtapi_credentials")) == 1 and len(await q(ctx, "SELECT 1 FROM mtapi_history_intervals")) == 0
        # a telegram/user id in the body is not an identity
        res = await ctx.http.post(f"{P}/{cid}/pause", headers=bob, json={"telegramUserId": ALICE, "user_id": ALICE})
        assert res.status_code == 404

    run_db(ctx, scenario)


def test_same_external_account_cannot_be_connected_twice(ctx):
    async def scenario():
        assert (await connect(ctx)).status_code == 200
        ctx.broker.accounts[TOKEN_B] = ctx.broker.accounts[TOKEN_A]   # Bob holds another valid token for the SAME broker account
        res = await connect(ctx, user=BOB, token=TOKEN_B)
        assert res.status_code == 409 and ALICE not in res.text and "700001" not in res.text   # generic, leaks nothing about the owner
        assert (await connect(ctx)).status_code == 409                                         # also for the owner: rotate instead
        assert [r[0] for r in await q(ctx, "SELECT user_id FROM mtapi_connections")] == [ALICE]
        assert len(await q(ctx, "SELECT 1 FROM mtapi_credentials")) == 1

    run_db(ctx, scenario)


def test_concurrent_connects_bind_exactly_once(ctx):
    async def scenario():
        identity = {"platform_host": "h.example", "external_login": "1", "broker_name": "B", "platform_url": "https://h.example", "account_currency": "USD", "account_type": "DEMO"}

        async def attempt(user):
            try:
                await ctx.store.create_connection(user_id=user, identity=identity, capabilities={}, status="active", status_detail=None,
                                                  linked_external_account_id=None, token_hint="...", encrypt=lambda cid: "v1.x.y", key_version=lambda b: "v1")
                return "ok"
            except IdentityConflict:
                return "conflict"

        results = await asyncio.gather(*(attempt(u) for u in (ALICE, BOB, "3003", "4004")))
        assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]
        assert len(await q(ctx, "SELECT 1 FROM mtapi_connections")) == 1 and len(await q(ctx, "SELECT 1 FROM mtapi_credentials")) == 1

    run_db(ctx, scenario)


def test_same_login_on_a_different_platform_is_a_different_account(ctx):
    async def scenario():
        ctx.broker.add_account(TOKEN_B, login="700001", platform_url="https://mtr.other-broker.example", broker_name="Broker One")
        assert (await connect(ctx)).status_code == 200
        res = await connect(ctx, user=BOB, token=TOKEN_B)
        assert res.status_code == 200   # same login + same display name, different verified platform: no collision, no merge
        assert sorted(r[0] for r in await q(ctx, "SELECT platform_host FROM mtapi_connections")) == ["mtr.broker-one.example", "mtr.other-broker.example"]

    run_db(ctx, scenario)


def test_broker_token_does_not_take_over_existing_talitrade_records(ctx):
    async def scenario():
        ctx.owners["700001"] = {BOB}      # legacy TaliTrade account with this number belongs to someone else
        body = (await connect(ctx)).json()["connection"]
        assert body["status"] == "active" and body["linked_account_id"] is None
        rec = (await ctx.http.get(f"{P}/{body['id']}/reconciliation", headers=auth(ALICE))).json()
        assert rec["report"] is None and rec["linked_account_id"] is None
        await ctx.http.delete(f"{P}/{body['id']}", headers=auth(ALICE))
        ctx.owners["700001"] = {ALICE}    # when the user already owns it, it is linked
        assert (await connect(ctx)).json()["connection"]["linked_account_id"] == "700001"

    run_db(ctx, scenario)


# ── rotation, generations, leases ────────────────────────────────────────────
def test_rotation_requires_the_same_account_and_fences_old_workers(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        wrong = await ctx.http.post(f"{P}/{cid}/rotate", headers=auth(ALICE), json={"token": TOKEN_B})
        assert wrong.status_code == 409
        assert (await q(ctx, "SELECT generation, token_hint FROM mtapi_connections"))[0] == (1, "...a1b2")

        old_gen = await ctx.store.acquire_lease(cid, "old-worker", 60)   # a worker is mid-flight with generation 1
        ctx.broker.accounts[TOKEN_B] = ctx.broker.accounts[TOKEN_A]      # replacement token for the same account
        ok = await ctx.http.post(f"{P}/{cid}/rotate", headers=auth(ALICE), json={"token": TOKEN_B})
        assert ok.status_code == 200 and TOKEN_B not in ok.text and ok.json()["connection"]["token_hint"] == "...c3d4"
        assert (await q(ctx, "SELECT generation FROM mtapi_connections"))[0][0] == 2
        from app.services.matchtrader_api import schemas
        summary = schemas.AccountSummary.model_validate({"balance": 1, "equity": 1})
        with pytest.raises(StaleWriter):
            await ctx.store.save_summary(cid, old_gen, "old-worker", summary, now_utc())
        with pytest.raises(StaleWriter):
            await ctx.store.replace_positions(cid, old_gen, "old-worker", [], now_utc())
        assert await q(ctx, "SELECT 1 FROM mtapi_account_summary") == []
        out = await sync(ctx, cid)
        assert out["summary"] == "ok" and ctx.broker.requests[-1][3] == TOKEN_B   # new token in use

    run_db(ctx, scenario)


def test_single_writer_lease_and_takeover(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        gen = await ctx.store.acquire_lease(cid, "w1", 60)
        assert gen == 1 and await ctx.store.acquire_lease(cid, "w2", 60) is None       # busy
        assert await sync(ctx, cid, owner="w2") == {"skipped": "busy"}
        assert await ctx.store.acquire_lease(cid, "w1", 60) == 1                         # owner can renew
        async with ctx.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET lease_expires_at = NOW() - INTERVAL '1 second'"))   # w1 died
        assert await ctx.store.acquire_lease(cid, "w2", 60) == 1                         # takeover after expiry
        from app.services.matchtrader_api import schemas
        with pytest.raises(StaleWriter):                                                  # w1 comes back: fenced
            await ctx.store.replace_positions(cid, gen, "w1", [schemas.OpenPosition.model_validate(position("1"))], now_utc())
        await ctx.store.release_lease(cid, "w1")                                         # owner-safe release: no effect
        assert (await q(ctx, "SELECT lease_owner FROM mtapi_connections"))[0][0] == "w2"

    run_db(ctx, scenario)


# ── live sync ────────────────────────────────────────────────────────────────
def test_positions_are_replaced_only_from_a_successful_full_response(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        account = ctx.broker.accounts[TOKEN_A]
        account["positions"] = [position("1"), position("2", symbol="XAUUSD", side=None)]
        assert (await sync(ctx, cid, history=False))["positions"] == "ok"
        rows = await q(ctx, "SELECT external_id, side, volume FROM mtapi_positions ORDER BY external_id")
        assert rows == [("1", "BUY", Decimal("0.1")), ("2", None, Decimal("0.1"))]   # null side stays null
        assert all("symbol" not in r[2] for r in ctx.broker.calls("/v1/positions"))   # always unfiltered

        for failure in ((500, {}), (403, {}), (429, {"Retry-After": "1"})):
            ctx.broker.fail["/v1/positions"] = failure
            ratelimit._budgets.clear()
            out = await sync(ctx, cid, history=False)
            assert out["positions"] in ("transient", "forbidden", "rate_limited") and out["summary"] == "ok"
            assert len(await q(ctx, "SELECT 1 FROM mtapi_positions")) == 2            # never cleared by a failure
        ctx.broker.fail.clear()
        ratelimit._budgets.clear()
        account["positions"] = "not-a-list"                                           # broken envelope
        assert (await sync(ctx, cid, history=False))["positions"] == "contract"
        assert len(await q(ctx, "SELECT 1 FROM mtapi_positions")) == 2
        state = (await ctx.store.get_sync_state(cid))["positions"]
        assert state["last_error_code"] == "contract" and state["consecutive_failures"] >= 1 and state["last_success_at"] is not None

        account["positions"] = [position("2", symbol="XAUUSD")]                       # a real, validated change
        assert (await sync(ctx, cid, history=False))["positions"] == "ok"
        assert [r[0] for r in await q(ctx, "SELECT external_id FROM mtapi_positions")] == ["2"]
        assert await q(ctx, "SELECT 1 FROM mtapi_closed_positions") == []             # disappearance does not invent a closed trade
        # an older snapshot arriving late must not overwrite newer state
        gen = await ctx.store.acquire_lease(cid, "late", 60)
        assert await ctx.store.replace_positions(cid, gen, "late", [], now_utc() - timedelta(minutes=5)) is False
        assert len(await q(ctx, "SELECT 1 FROM mtapi_positions")) == 1

    run_db(ctx, scenario)


def test_summary_is_stored_with_exact_decimals_and_zero_margin(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.accounts[TOKEN_A]["summary"] = {"balance": 10000.12, "credit": 0, "equity": 10010.62, "margin": 0, "freeFunds": 10010.62, "marginLevel": 0, "profit": 10.5}
        await sync(ctx, cid, history=False)
        row = (await q(ctx, "SELECT balance, equity, margin, free_funds, margin_level FROM mtapi_account_summary"))[0]
        assert row == (Decimal("10000.12"), Decimal("10010.62"), Decimal("0"), Decimal("10010.62"), Decimal("0"))

    run_db(ctx, scenario)


def test_unauthorized_stops_the_connection_and_forbidden_disables_one_capability(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.fail["/v1/ledgers"] = (403, {})
        out = await sync(ctx, cid)
        assert out["ledgers"] == "forbidden" and out["closed_positions"] == "ok" and out["positions"] == "ok"   # others keep working
        ctx.broker.fail.clear()
        del ctx.broker.accounts[TOKEN_A]                    # token revoked/expired at the broker
        out = await sync(ctx, cid)
        assert out == {"summary": "unauthorized"}           # stops at the first 401
        assert (await q(ctx, "SELECT status, status_detail FROM mtapi_connections"))[0] == ("needs_reauth", "unauthorized")
        before = len(ctx.broker.requests)
        assert await sync(ctx, cid) == {"skipped": "needs_reauth"}
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0
        assert len(ctx.broker.requests) == before           # no retry storm on a dead token
        listing = (await ctx.http.get(P, headers=auth(ALICE))).json()["connections"][0]
        assert listing["status"] == "needs_reauth" and listing["freshness"]["summary"]["last_success_at"] is not None

    run_db(ctx, scenario)


# ── history ──────────────────────────────────────────────────────────────────
def test_partial_closes_replays_corrections_and_between_poll_trades(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        account = ctx.broker.accounts[TOKEN_A]
        t = NOW - 3_600_000
        account["closed"] = [closed("u1", "P1", t, "10.5"), closed("u2", "P1", t + 1000, "4.25"),      # two partial closes of P1
                             closed("u3", "P9", t + 2000, "-3")]                                         # opened and closed between polls
        account["ledgers"] = [{"id": "L1", "comment": "", "amount": 10.5, "timestamp": t, "type": "CLOSE_POSITION"},
                              {"id": "L2", "comment": "", "amount": 500.0, "timestamp": t, "type": "DEPOSIT"}]
        assert (await sync(ctx, cid))["closed_positions"] == "ok"
        rows = await q(ctx, "SELECT uid, position_id, net_profit, revision FROM mtapi_closed_positions ORDER BY uid")
        assert rows == [("u1", "P1", Decimal("10.5"), 1), ("u2", "P1", Decimal("4.25"), 1), ("u3", "P9", Decimal("-3"), 1)]
        assert await q(ctx, "SELECT 1 FROM mtapi_positions") == []          # u3 never appeared as an open position, still imported

        for _ in range(3):                                                    # replays / overlapping windows
            await sync(ctx, cid)
        assert (await q(ctx, "SELECT COUNT(*), SUM(revision) FROM mtapi_closed_positions"))[0] == (3, 3)
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_ledgers"))[0][0] == 2

        account["closed"][0] = closed("u1", "P1", t, "9.75")                 # late correction, an hour old: outside the 15 min overlap
        await sync(ctx, cid, now=NOW + 60_000)
        assert (await q(ctx, "SELECT revision FROM mtapi_closed_positions WHERE uid = 'u1'"))[0][0] == 1   # not seen by the narrow window...
        async with ctx.db.begin() as conn:                                   # ...until the periodic wide re-check is due again
            await conn.execute(text("UPDATE mtapi_history_intervals SET created_at = NOW() - INTERVAL '7 hours' WHERE kind = 'reconcile'"))
        await sync(ctx, cid, now=NOW + 120_000)
        row = (await q(ctx, "SELECT net_profit, revision, previous_raw->>'netProfit' FROM mtapi_closed_positions WHERE uid = 'u1'"))[0]
        assert row == (Decimal("9.75"), 2, "10.5")                           # updated in place, old value kept as provenance
        # trade P&L and ledger cash movements are separate facts and are never added together
        total = (await q(ctx, "SELECT SUM(net_profit) FROM mtapi_closed_positions"))[0][0]
        assert total == Decimal("11.00")
        assert (await q(ctx, "SELECT entry_type, amount FROM mtapi_ledgers ORDER BY external_id")) == [("CLOSE_POSITION", Decimal("10.5")), ("DEPOSIT", Decimal("500.0"))]

    run_db(ctx, scenario)


def test_backfill_bisects_saturated_ranges_survives_restart_and_reports_coverage(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_BACKFILL_INTERVALS_PER_CYCLE", "3")

    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        start = NOW - 400 * 86_400_000
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed(f"u{i}", f"P{i}", start + i * 3_600_000) for i in range(1300)]
        res = await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": start})
        assert res.status_code == 200 and res.json()["queued"] == ["closed_positions", "ledgers"]
        assert (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": start})).json()["queued"] == []   # no duplicate tasks

        await sync(ctx, cid, live=False)                                   # first pass: bounded work only
        partial = (await ctx.store.coverage(cid))["closed_positions"]
        assert partial["status"] == "in_progress" and 0 < partial["records"] < 1300 and partial["queued_intervals"] > 0

        passes = 0
        while (await ctx.store.coverage(cid))["closed_positions"]["queued_intervals"] and passes < 60:
            ratelimit._budgets.clear()                                      # stand-in for time passing between passes
            await sync(ctx, cid, live=False, owner=f"restart-{passes}")    # a fresh worker each time == restart safety
            passes += 1
        cov = (await ctx.store.coverage(cid))["closed_positions"]
        assert cov["records"] == 1300 and cov["gap_count"] == 0 and cov["status"] == "complete_for_requested_range"
        assert cov["requested_from_ms"] == start - 1
        assert (await q(ctx, "SELECT COUNT(DISTINCT uid) FROM mtapi_closed_positions"))[0][0] == 1300
        assert all(int(r[2]["limit"]) == 500 for r in ctx.broker.calls("/v1/closed-positions") if "limit" in r[2] and r[2]["limit"] != "1")
        listing = (await ctx.http.get(P, headers=auth(ALICE))).json()["connections"][0]
        assert listing["history_coverage"]["closed_positions"]["status"] == "complete_for_requested_range"

    run_db(ctx, scenario)


def test_backfill_gap_is_reported_and_never_called_complete(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        t = NOW - 10 * 86_400_000
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed(f"d{i}", f"P{i}", t) for i in range(620)]   # 620 closes in ONE millisecond
        await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": t - 1000})
        for n in range(80):
            ratelimit._budgets.clear()
            await sync(ctx, cid, live=False, owner=f"w{n}")
            if not (await ctx.store.coverage(cid))["closed_positions"]["queued_intervals"]:
                break
        cov = (await ctx.store.coverage(cid))["closed_positions"]
        assert cov["status"] == "partial_gaps" and cov["gaps_ms"] == [[t, t]] and cov["records"] == 500   # kept what was returned

    run_db(ctx, scenario)


def test_failed_history_transaction_is_replayed_not_skipped(ctx, monkeypatch):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed("u1", "P1", NOW - 1000)]
        original = SqlStore._upsert_closed
        calls = {"n": 0}

        async def flaky(self, conn, connection_id, entries):
            calls["n"] += 1
            if calls["n"] == 1:
                await original(self, conn, connection_id, entries)
                raise RuntimeError("database went away mid-transaction")
            return await original(self, conn, connection_id, entries)

        monkeypatch.setattr(SqlStore, "_upsert_closed", flaky)
        out = await sync(ctx, cid, live=False)
        assert out["closed_positions"] == "internal"
        assert await q(ctx, "SELECT 1 FROM mtapi_closed_positions") == []                       # rolled back: nothing half-written
        assert (await q(ctx, "SELECT status FROM mtapi_history_intervals WHERE resource = 'closed_positions'"))[0][0] == "queued"
        out = await sync(ctx, cid, live=False)                                                  # next pass replays the same window
        assert out["closed_positions"] == "ok" and len(await q(ctx, "SELECT 1 FROM mtapi_closed_positions")) == 1

    run_db(ctx, scenario)


# ── pause / disconnect / worker ──────────────────────────────────────────────
def test_pause_resume_and_disconnect(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed("u1", "P1", NOW - 1000)]
        await sync(ctx, cid)
        gen = await ctx.store.acquire_lease(cid, "in-flight", 60)
        assert (await ctx.http.post(f"{P}/{cid}/pause", headers=auth(ALICE))).status_code == 200
        from app.services.matchtrader_api import schemas
        with pytest.raises(StaleWriter):
            await ctx.store.save_summary(cid, gen, "in-flight", schemas.AccountSummary.model_validate({"balance": 1, "equity": 1}), now_utc())
        before = len(ctx.broker.requests)
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0 and len(ctx.broker.requests) == before
        assert (await ctx.http.post(f"{P}/{cid}/pause", headers=auth(ALICE))).status_code == 409
        assert (await ctx.http.post(f"{P}/{cid}/resume", headers=auth(ALICE))).status_code == 200
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 1

        res = await ctx.http.delete(f"{P}/{cid}", headers=auth(ALICE))
        assert res.status_code == 200 and "does not revoke" in res.json()["note"]
        assert await q(ctx, "SELECT 1 FROM mtapi_credentials") == []                              # stored token deleted
        assert (await q(ctx, "SELECT status, token_hint FROM mtapi_connections"))[0] == ("disconnected", None)
        assert len(await q(ctx, "SELECT 1 FROM mtapi_closed_positions")) == 1                     # imported history retained
        before = len(ctx.broker.requests)
        assert await sync(ctx, cid) == {"skipped": "disconnected"}
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0
        assert len(ctx.broker.requests) == before                                                 # no API calls after disconnect
        assert (await ctx.http.get(P, headers=auth(ALICE))).json()["connections"] == []
        assert (await ctx.http.post(f"{P}/{cid}/sync", headers=auth(ALICE))).status_code == 404
        assert (await connect(ctx)).status_code == 200                                            # the account can be connected again

    run_db(ctx, scenario)


def test_worker_isolates_failures_between_accounts_and_reschedules(ctx):
    async def scenario():
        ctx.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        a = (await connect(ctx)).json()["connection"]["id"]
        b = (await connect(ctx, user=BOB, token=TOKEN_B)).json()["connection"]["id"]
        del ctx.broker.accounts[TOKEN_A]                                # Alice's token dies
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 2
        statuses = dict(await q(ctx, "SELECT id, status FROM mtapi_connections"))
        assert statuses == {a: "needs_reauth", b: "active"}
        assert (await ctx.store.get_sync_state(b))["summary"]["last_success_at"] is not None   # Bob's account still synced
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 0             # nothing due again immediately
        assert (await q(ctx, "SELECT next_live_sync_at > NOW() FROM mtapi_connections WHERE id = :id", id=b))[0][0] is True

    run_db(ctx, scenario)


def test_manual_sync_shares_the_rate_budget_and_reconciliation_is_read_only(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_GLOBAL_PER_MINUTE", "30")

    async def scenario():
        ctx.owners["700001"] = {ALICE}
        cid = (await connect(ctx)).json()["connection"]["id"]
        import time
        t = int(time.time() * 1000) - 5000        # the route uses the real clock
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed("u1", "P1", t, "24.5")]
        async with ctx.db.begin() as conn:
            await conn.execute(text("INSERT INTO trades (account_id, symbol, direction, volume, close_price, pnl, closed_at, source) VALUES ('700001','US30','buy',0.1,39050.5,25,to_timestamp(:t),'scraper')"), {"t": t // 1000})
        probes = len(ctx.broker.requests)
        bodies = [(await ctx.http.post(f"{P}/{cid}/sync", headers=auth(ALICE))).json() for _ in range(12)]
        assert bodies[0]["ok"] is True and bodies[0]["result"]["state"] == "ok" and bodies[0]["result"]["live_ready"] is True
        assert bodies[-1]["ok"] is False and bodies[-1]["result"]["state"] == "rate_limited"   # HTTP 200, but honestly not a refresh
        assert len(ctx.broker.requests) - probes <= 30                                         # account-wide cap; throttled calls were never sent
        rec = (await ctx.http.get(f"{P}/{cid}/reconciliation", headers=auth(ALICE))).json()
        assert rec["linked_account_id"] == "700001" and rec["report"]["matched"] == 1 and rec["report"]["writes_performed"] == 0
        assert rec["report"]["matched_detail"][0]["pnl_difference"] == "-0.5"
        assert (await q(ctx, "SELECT COUNT(*), SUM(pnl) FROM trades"))[0] == (1, 25.0)   # legacy data untouched

    run_db(ctx, scenario)


# ── dashboard read models ─────────────────────────────────────────────────────
FORBIDDEN_KEYS = ("ciphertext", "key_version", "generation", "lease", "raw", "revision", "token", "email", "external_login", "user_id")


def assert_no_internal_fields(payload):
    text_form = str(payload).lower()
    for key in FORBIDDEN_KEYS:
        assert f"'{key}" not in text_form, key
    assert TOKEN_A.lower() not in text_form and "700001" not in text_form   # full login is masked


def test_snapshot_separates_unknown_from_zero_and_empty(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        res = await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))
        assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
        first = res.json()
        assert first["connection"]["display_state"] == "first_sync_pending" and first["connection"]["account_key"] == f"mtapi:{cid}"
        assert first["connection"]["masked_login"] == "…0001" and first["connection"]["display_label"] == "Broker One · …0001"
        assert first["summary"] is None and first["positions"] is None and first["pending_orders"] is None   # never fetched != zero/empty
        assert first["freshness"]["summary"]["status"] == "never_synced" and first["history"]["closed_entry_count"] is None
        assert first["history"]["period_stats"] is None and first["history"]["coverage_status"] == "pending"
        assert_no_internal_fields(first)
        before = len(ctx.broker.requests)
        for _ in range(5):
            await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))
            await ctx.http.get(f"{P}/{cid}/trades", headers=auth(ALICE))
            await ctx.http.get(P, headers=auth(ALICE))
        assert len(ctx.broker.requests) == before            # reads never contact the broker

        account = ctx.broker.accounts[TOKEN_A]
        account["summary"] = {"balance": 98000.5, "credit": 0, "equity": 97990.25, "margin": 0, "freeFunds": 97990.25, "marginLevel": 0, "profit": -10.25}
        await sync(ctx, cid)
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["connection"]["display_state"] == "ready"
        assert snap["summary"]["balance"] == "98000.5" and snap["summary"]["margin"] == "0" and snap["summary"]["profit"] == "-10.25"   # strings, sign kept, zero is a value
        assert snap["positions"] == [] and snap["pending_orders"] == []                                                        # fetched and empty
        assert snap["freshness"]["positions"]["status"] == "fresh" and snap["freshness"]["positions"]["is_stale"] is False
        assert snap["history"]["closed_entry_count"] == 0 and snap["history"]["period_stats"]["net_profit"] == "0"
        assert snap["background_refresh"]["enabled"] is False     # worker flag is off in this test: the UI can say so
        card = (await ctx.http.get(P, headers=auth(ALICE))).json()["connections"][0]["card"]
        assert card["balance"] == "98000.5" and card["account_key"] == f"mtapi:{cid}"
        assert_no_internal_fields(snap)

        account["positions"] = [position("1", symbol="<img src=x onerror=alert(1)>", side=None, slPrice=None)]
        await sync(ctx, cid, now=NOW + 1000, history=False)
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["positions"][0]["side"] is None and snap["positions"][0]["sl_price"] is None and snap["positions"][0]["volume"] == "0.1"
        assert snap["positions"][0]["symbol"] == "<img src=x onerror=alert(1)>"   # returned verbatim as JSON data; the page must escape it

    run_db(ctx, scenario)


def test_snapshot_reports_stale_failed_and_forbidden_states(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.fail["/v1/ledgers"] = (403, {})
        await sync(ctx, cid)
        ctx.broker.fail["/v1/positions"] = (500, {})
        ratelimit._budgets.clear()
        await sync(ctx, cid, now=NOW + 1000, history=False)
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["freshness"]["positions"]["status"] == "degraded" and snap["freshness"]["positions"]["last_error"] == "transient"
        assert snap["positions"] == []                                 # last good value is kept
        assert snap["freshness"]["ledgers"]["last_error"] == "forbidden"
        async with ctx.db.begin() as conn:                             # time passes with no successful refresh
            await conn.execute(text("UPDATE mtapi_sync_state SET last_success_at = NOW() - INTERVAL '10 minutes' WHERE resource IN ('summary','positions')"))
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["freshness"]["summary"]["status"] == "stale" and snap["freshness"]["summary"]["is_stale"] is True
        assert snap["connection"]["display_state"] == "delayed" and snap["summary"] is not None   # shown, but marked stale
        del ctx.broker.accounts[TOKEN_A]
        ctx.broker.fail.clear()
        ratelimit._budgets.clear()
        await sync(ctx, cid, now=NOW + 2000)
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["connection"]["status"] == "needs_reauth" and snap["connection"]["display_state"] == "needs_reauth"

    run_db(ctx, scenario)


def test_read_models_are_owner_scoped_and_hide_disconnected(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed(f"u{i}", "P1", NOW - 1000 - i) for i in range(5)]
        await sync(ctx, cid)
        mine = (await ctx.http.get(f"{P}/{cid}/trades?limit=2", headers=auth(ALICE))).json()
        assert len(mine["trades"]) == 2 and mine["next_cursor"]
        bob = auth(BOB)
        for path in (f"{P}/{cid}/snapshot", f"{P}/{cid}/trades", f"{P}/{cid}/trades?cursor={mine['next_cursor']}", f"{P}/999999/snapshot", f"{P}/999999/trades"):
            res = await ctx.http.get(path, headers=bob)
            assert res.status_code == 404 and res.json() == {"detail": "Connection not found"}, path   # a stolen cursor does not help
            assert "u0" not in res.text
        for path in (f"{P}/{cid}/snapshot", f"{P}/{cid}/trades"):
            assert (await ctx.http.get(path)).status_code == 401
            assert (await ctx.http.get(path, headers={"Authorization": "Bearer tti_extension"})).status_code == 401
        from app.services.matchtrader_api.read_models import NotFound, ReadModels
        rm = ReadModels(lambda: ctx.db)
        with pytest.raises(NotFound):
            await rm.read_owned_snapshot(cid, BOB)             # enforced in SQL, not only in the route
        with pytest.raises(NotFound):
            await rm.read_owned_closed_trades(cid, BOB, limit=10, cursor=None)
        assert await rm.list_owned(BOB) == []
        await ctx.http.delete(f"{P}/{cid}", headers=auth(ALICE))
        for path in (f"{P}/{cid}/snapshot", f"{P}/{cid}/trades"):
            assert (await ctx.http.get(path, headers=auth(ALICE))).status_code == 404   # disconnected: gone, even for the owner
        with pytest.raises(NotFound):
            await rm.read_owned_snapshot(cid, ALICE)

    run_db(ctx, scenario)


def test_trade_pages_are_stable_complete_and_aggregates_use_the_whole_set(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        t = NOW - 3_600_000
        rows = [closed(f"u{i:03d}", f"P{i // 2}", t + (i // 3) * 1000, net=str(5 if i % 4 else -7)) for i in range(57)]   # ties on close time; partial closes share P ids
        ctx.broker.accounts[TOKEN_A]["closed"] = list(rows)
        await sync(ctx, cid)
        seen, cursor, pages = [], None, 0
        while True:
            url = f"{P}/{cid}/trades?limit=10" + (f"&cursor={cursor}" if cursor else "")
            page = (await ctx.http.get(url, headers=auth(ALICE))).json()
            seen += [r["uid"] for r in page["trades"]]
            pages += 1
            if pages == 2:      # a refresh / new trade arriving mid-pagination must not shift later pages
                ctx.broker.accounts[TOKEN_A]["closed"].append(closed("newest", "PX", NOW - 10, net="1"))
                await sync(ctx, cid, now=NOW + 5000)
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert len(seen) == 57 and len(set(seen)) == 57 and "newest" not in seen   # no duplicates, no gaps
        times = [r["closeTime"] for r in sorted(rows, key=lambda r: (r["closeTime"], r["uid"]), reverse=True)]
        assert seen == [r["uid"] for r in sorted(rows, key=lambda r: (r["closeTime"], r["uid"]), reverse=True)] and times == sorted(times, reverse=True)
        first = (await ctx.http.get(f"{P}/{cid}/trades?limit=3", headers=auth(ALICE))).json()["trades"][0]
        assert first["uid"] == "newest" and first["entry_key"] == f"mtapi:{cid}:newest" and first["net_profit"] == "1" and first["close_time"].endswith("Z")
        assert set(first) == {"entry_key", "uid", "position_id", "symbol", "alias", "side", "volume", "open_price", "close_price", "open_time", "close_time",
                              "profit", "commission", "swap", "net_profit", "close_reason"}
        assert (await ctx.http.get(f"{P}/{cid}/trades?limit=100000", headers=auth(ALICE))).status_code == 200
        assert len((await ctx.http.get(f"{P}/{cid}/trades?limit=100000", headers=auth(ALICE))).json()["trades"]) == 58   # capped at 100, all 58 fit
        for bad in ("garbage", "W10", "%27%3B+DROP+TABLE"):
            assert (await ctx.http.get(f"{P}/{cid}/trades?cursor={bad}", headers=auth(ALICE))).status_code == 400
        stats = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]
        expected_net = sum(5 if i % 4 else -7 for i in range(57)) + 1
        assert stats["closed_entry_count"] == 58 and stats["period_stats"]["net_profit"] == str(expected_net)     # whole set, not one page
        assert stats["period_stats"]["winning_entries"] == 43 and stats["period_stats"]["losing_entries"] == 15
        assert stats["period_stats"]["positions"] == 30                                                             # 57 entries over 29 positions + 1

    run_db(ctx, scenario)


def test_first_sync_flow_connect_sync_backfill_then_worker_keeps_refreshing(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_WORKER_ENABLED", "true")

    async def scenario():
        import time
        real_now = int(time.time() * 1000)
        account = ctx.broker.accounts[TOKEN_A]
        account["closed"] = [closed("recent", "P1", real_now - 3_600_000, net="12.5"), closed("older", "P2", real_now - 5 * 86_400_000, net="-4")]
        cid = (await connect(ctx)).json()["connection"]["id"]
        first = (await ctx.http.post(f"{P}/{cid}/sync", headers=auth(ALICE))).json()
        assert first["ok"] is True and first["result"]["live_ready"] is True
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["summary"]["balance"] == "10000.12" and snap["history"]["closed_entry_count"] == 1     # only the last 24h so far
        week = real_now - 7 * 86_400_000
        queued = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": week})).json()
        assert queued["queued"] == ["closed_positions", "ledgers"] and queued["requested_to"] and queued["background_refresh_enabled"] is True
        again = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": week})).json()
        assert again["queued"] == []                                                                       # idempotent
        assert (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]["coverage_status"] == "in_progress"

        # Browser closed: only the worker runs from here.
        async with ctx.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET next_live_sync_at = NOW(), next_history_sync_at = NOW()"))
        before = (await ctx.store.get_sync_state(cid))["summary"]["last_success_at"]
        account["summary"]["balance"] = 10111.0
        assert await worker.run_due_once(ctx.store, client_factory=ctx.factory) == 1
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["summary"]["balance"] == "10111" and (await ctx.store.get_sync_state(cid))["summary"]["last_success_at"] > before
        assert snap["history"]["closed_entry_count"] == 2 and snap["history"]["coverage_status"] == "complete_for_requested_range"
        assert snap["history"]["period_stats"]["net_profit"] == "8.5" and snap["background_refresh"]["enabled"] is True
        trades = (await ctx.http.get(f"{P}/{cid}/trades", headers=auth(ALICE))).json()["trades"]
        assert [t["uid"] for t in trades] == ["recent", "older"]
        # Reopening the app needs no token: the connection is restored from the server.
        restored = (await ctx.http.get(P, headers=auth(ALICE))).json()["connections"]
        assert len(restored) == 1 and restored[0]["card"]["balance"] == "10111" and TOKEN_A not in str(restored)

    run_db(ctx, scenario)


def test_connect_can_succeed_while_the_first_sync_fails(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        for path in ("/v1/account-summary", "/v1/positions", "/v1/pending-orders", "/v1/closed-positions", "/v1/ledgers"):
            ctx.broker.fail[path] = (503, {})
        body = (await ctx.http.post(f"{P}/{cid}/sync", headers=auth(ALICE))).json()
        assert body["ok"] is False and body["result"]["state"] == "failed" and body["result"]["live_ready"] is False
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()
        assert snap["connection"]["status"] == "active" and snap["connection"]["display_state"] == "first_sync_pending"
        assert snap["summary"] is None and snap["freshness"]["summary"]["status"] == "failed" and snap["freshness"]["summary"]["last_error"] == "transient"
        gen = await ctx.store.acquire_lease(cid, "worker-mid-pass", 60)   # the worker is busy with this account
        assert gen is not None
        busy = (await ctx.http.post(f"{P}/{cid}/sync", headers=auth(ALICE))).json()
        assert busy["ok"] is False and busy["result"]["state"] == "busy"   # not reported as a successful refresh

    run_db(ctx, scenario)
