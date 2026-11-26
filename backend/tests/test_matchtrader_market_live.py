"""Live US30 on the Charts tab: symbol resolution from the account's own instruments, one shared
quote poll per account, candle price-side verification, out-of-order quotes, the rollout flag and
the rate budget. Synthetic instruments and prices only; the broker is a scripted fake.

The database tests need TALITRADE_TEST_DATABASE_URL (a disposable database) and are skipped otherwise.
"""
import os
import time
from decimal import Decimal

import pytest
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.matchtrader_api import config, market, ratelimit  # noqa: E402
from tests.mt_fakes import TOKEN_A, TOKEN_B, market_candle, market_quote, market_symbol  # noqa: E402
from tests.test_matchtrader_api_db import ALICE, BOB, P, auth, connect, ctx, pytestmark as needs_database, q, run_db  # noqa: E402,F401
from tests.test_matchtrader_market import calls, make_due, market_round  # noqa: E402

D = Decimal


def sym(name, alias=None, **kw):
    return {"symbol": name, "alias": alias or name, "description": f"{name} synthetic", "type": "CFD", "session_open": True, **kw}


# ── symbol resolution ────────────────────────────────────────────────────────
def test_us30_resolves_from_the_account_list_including_the_metatrader_name():
    assert market.resolve_instrument([sym("EURUSD"), sym("DJI30")], "US30")["symbol"]["symbol"] == "DJI30"     # MetaTrader's name
    assert market.resolve_instrument([sym("US30")], "US30")["symbol"]["symbol"] == "US30"
    assert market.resolve_instrument([sym("#DOW", alias="US30")], "US30")["symbol"]["symbol"] == "#DOW"       # alias counts
    assert market.resolve_instrument([sym("US30.cash")], "US30")["symbol"]["symbol"] == "US30.cash"
    assert market.resolve_instrument([sym("US30m"), sym("NAS100")], "US30")["symbol"]["symbol"] == "US30m"     # one broker suffix
    exact_wins = market.resolve_instrument([sym("US30"), sym("US30.f")], "US30")
    assert exact_wins["status"] == "resolved" and exact_wins["symbol"]["symbol"] == "US30"                     # exact name beats a suffixed one


def test_missing_or_ambiguous_us30_is_reported_never_guessed():
    two_exact = market.resolve_instrument([sym("US30"), sym("DJI30")], "US30")
    assert two_exact["status"] == "ambiguous" and two_exact["symbol"] is None
    assert [c["symbol"] for c in two_exact["candidates"]] == ["US30", "DJI30"]
    assert market.resolve_instrument([sym("US30m"), sym("US30.f")], "US30")["status"] == "ambiguous"
    for offered in ([sym("NAS100"), sym("EURUSD")], [sym("US300")], [sym("US3000")], [sym("XUS30")], []):
        assert market.resolve_instrument(offered, "US30")["status"] == "missing", offered               # no digits as a "suffix", no substrings
    assert market.resolve_instrument([sym("US30"), sym("US30")], "US30")["status"] == "resolved"        # a repeated row is one instrument


# ── price side ───────────────────────────────────────────────────────────────
def test_candle_price_side_is_only_claimed_after_agreeing_samples():
    assert market.basis_sample(D("44100.5"), D("44100.5"), D("44102.5")) == "bid"
    assert market.basis_sample(D("44102.5"), D("44100.5"), D("44102.5")) == "ask"
    assert market.basis_sample(D("44101.5"), D("44100.5"), D("44102.5")) == "mid"
    assert market.basis_sample(D("44107"), D("44100.5"), D("44102.5")) == "miss"         # price moved between the two requests
    assert market.basis_sample(D("1"), D("1"), D("1")) == "miss"                          # zero spread proves nothing
    assert market.basis_sample(D("1"), None, D("1")) == "miss"
    assert market.basis_verdict(None) == "unverified"
    assert market.basis_verdict({"bid_hits": 2}) == "unverified"                          # not enough yet
    assert market.basis_verdict({"bid_hits": 3, "misses": 40}) == "bid"                  # misses are not evidence against
    assert market.basis_verdict({"bid_hits": 9, "ask_hits": 1}) == "bid"
    assert market.basis_verdict({"bid_hits": 5, "ask_hits": 4}) == "unverified"          # contradicting evidence: nothing applied


# ── rate budget ──────────────────────────────────────────────────────────────
def test_live_chart_budget_sits_between_background_work_and_account_sync(monkeypatch):
    monkeypatch.setenv(ratelimit.GLOBAL_PER_MINUTE_ENV, "30")
    clock = [0.0]

    def fill(**kw):
        budget, n = ratelimit.AccountRateBudget(clock=lambda: clock[0]), 0
        try:
            while True:
                budget.reserve("/v1/quotes", **kw)
                n += 1
        except ratelimit.BudgetExhausted:
            return n
    assert fill(low_priority=True) == 15 and fill(live=True) == 24 and fill() == 30        # account sync always keeps the last fifth
    shared = ratelimit.AccountRateBudget(clock=lambda: clock[0])
    for _ in range(24):
        shared.reserve("/v1/quotes", live=True)
    with pytest.raises(ratelimit.BudgetExhausted):
        shared.reserve("/v1/quotes", live=True)
    shared.reserve("/v1/positions")                                                        # sync still gets through
    assert config.market_live_quote_interval_seconds() == 6 and 60 / 6 <= 24 - 7           # 10 quote requests/min next to ~7 sync calls


# ── against PostgreSQL ───────────────────────────────────────────────────────
def now_minute_ms() -> int:
    return int(time.time()) // 60 * 60 * 1000


def seed_us30(account, *, bid=44100.5, ask=44102.5, close=None, provider_ms=None, session_open=True, name="DJI30"):
    minute = now_minute_ms()
    account["symbols"] = [market_symbol("EURUSD", session_open=session_open),
                          market_symbol(name, type_="CFD", base="USD", quote="USD", precision=1, session_open=session_open)]
    account["quotes"] = {"EURUSD": market_quote("EURUSD", 1.08512, 1.08519, minute),
                         name: market_quote(name, bid, ask, provider_ms or minute + 5_000)}
    account["candles"] = {(name, "M1"): [market_candle(minute - (4 - i) * 60_000, 44090 + i, 44110 + i, 44080 + i, 44095 + i) for i in range(4)]
                          + [market_candle(minute, 44099, 44104, 44097, bid if close is None else close)]}   # the forming candle closes on the bid


@pytest.fixture
def live(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("TALITRADE_MT_API_MARKET_LIVE_ENABLED", "true")
    seed_us30(ctx.broker.accounts[TOKEN_A])
    ratelimit._budgets.clear()
    return ctx


@needs_database
def test_us30_live_quotes_one_shared_request_per_interval_for_every_tab(live):
    async def scenario():
        cid = (await connect(live)).json()["connection"]["id"]
        first = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert first["resolution"]["status"] == "pending" and first["status"] == "loading" and first["mode"] == "polling"
        await live.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        await market_round(live)                                                       # instruments (+ ticker)
        body = (await live.http.get(f"{P}/{cid}/market/live?instrument=US30", headers=auth(ALICE))).json()
        assert body["resolution"]["status"] == "resolved" and body["resolution"]["symbol"]["symbol"] == "DJI30"
        before = len(calls(live, "/v1/quotes"))
        await make_due(live)
        await market_round(live)
        assert len(calls(live, "/v1/quotes")) == before + 1                              # ticker and live chart: ONE request
        assert [qs for p, qs in live.broker.queries if p == "/v1/quotes"][-1] == "symbol=EURUSD&symbol=DJI30"
        body = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert body["status"] == "fresh" and body["quote"]["bid"] == "44100.5" and body["quote"]["ask"] == "44102.5"
        assert body["quote"]["provider_time_ms"] % 60_000 == 5_000 and body["quote"]["provider_time"].endswith(":05Z")   # the broker's tick time, in ms
        assert body["poll_interval_seconds"] == 6 and body["price_basis"]["side"] == "unverified"
        assert TOKEN_A not in str(body) and "_wake_worker" not in body

        # Ten tabs reading: the cache answers, one live feed row, the next poll is ~6 s away.
        for _ in range(10):
            await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        assert len(calls(live, "/v1/quotes")) == before + 1
        (rows_, wait), = await q(live, "SELECT COUNT(*), MAX(EXTRACT(EPOCH FROM next_due_at - NOW())) FROM mtapi_market_feeds WHERE key LIKE 'live:%'")
        assert rows_ == 1 and 3 <= float(wait) <= 6.5
        assert await market_round(live) == 0                                             # not due yet: no broker call
        (ticker_wait,), = await q(live, "SELECT EXTRACT(EPOCH FROM next_due_at - NOW()) FROM mtapi_market_feeds WHERE key = 'quotes'")
        assert float(ticker_wait) > 10                                                   # the ticker was refreshed by the live request

        # Interval 6 s, simulated for one minute: about 10 quote requests, never more than the live share of the budget.
        start = len(calls(live, "/v1/quotes"))
        for _ in range(10):
            async with live.db.begin() as conn:
                await conn.execute(text("UPDATE mtapi_market_feeds SET next_due_at = NOW() WHERE key LIKE 'live:%'"))
            await market_round(live)
        assert len(calls(live, "/v1/quotes")) - start == 10

    run_db(live, scenario)


@needs_database
def test_candle_price_side_is_verified_from_same_pass_samples_before_quotes_may_move_candles(live):
    async def scenario():
        cid = (await connect(live)).json()["connection"]["id"]
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await market_round(live)
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))               # live feed registered for DJI30
        await live.http.get(f"{P}/{cid}/market/candles?symbol=DJI30&interval=M1", headers=auth(ALICE))
        for _ in range(3):
            seed_us30(live.broker.accounts[TOKEN_A])                                     # forming candle in the current minute
            await make_due(live)
            mark = len(live.broker.requests)
            await market_round(live)
            paths = [r[1] for r in live.broker.requests[mark:]]
            assert paths.count("/v1/quotes") == 1 and paths.count("/v1/candles") == 1     # candle and quote in the same pass
        body = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert body["price_basis"]["side"] == "bid" and body["price_basis"]["samples"]["bid"] >= 3 and body["price_basis"]["samples"]["ask"] == 0
        chart = (await live.http.get(f"{P}/{cid}/market/candles?symbol=DJI30&interval=M1", headers=auth(ALICE))).json()
        assert chart["symbol"]["symbol"] == "DJI30" and len(chart["candles"]) == 5          # history still comes from the broker's candles

        # A price that matches the ask instead would be contradicting evidence: the verdict is withdrawn, not flipped.
        for _ in range(3):
            seed_us30(live.broker.accounts[TOKEN_A], close=44102.5)
            await make_due(live)
            await market_round(live)
        body = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert body["price_basis"]["side"] == "unverified" and body["price_basis"]["samples"]["ask"] >= 3

    run_db(live, scenario)


@needs_database
def test_an_older_or_out_of_order_quote_never_replaces_a_newer_one(live):
    async def scenario():
        cid = (await connect(live)).json()["connection"]["id"]
        await live.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await market_round(live)
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await make_due(live)
        await market_round(live)
        async with live.db.begin() as conn:                                               # the ticker tab closes; its last EURUSD quote stays cached
            await conn.execute(text("UPDATE mtapi_market_feeds SET interest_until = NOW() - interval '1 second' WHERE key = 'quotes'"))
        newer = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()["quote"]
        account = live.broker.accounts[TOKEN_A]
        account["quotes"]["DJI30"] = market_quote("DJI30", 1.0, 2.0, newer["provider_time_ms"] - 3_000)   # late, older snapshot
        await make_due(live)
        await market_round(live)
        kept = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()["quote"]
        assert kept["bid"] == newer["bid"] and kept["provider_time_ms"] == newer["provider_time_ms"]
        account["quotes"]["DJI30"] = market_quote("DJI30", 44120.5, 44122.5, newer["provider_time_ms"] + 1_000)
        await make_due(live)
        await market_round(live)
        assert (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()["quote"]["bid"] == "44120.5"
        # Those live-only requests asked for DJI30 alone: they must not delete quotes they did not ask for.
        assert [qs for p, qs in live.broker.queries if p == "/v1/quotes"][-1] == "symbol=DJI30"
        (eurusd,), = await q(live, "SELECT COUNT(*) FROM mtapi_market_quotes WHERE symbol = 'EURUSD'")
        assert eurusd == 1

    run_db(live, scenario)


@needs_database
def test_ambiguous_or_missing_us30_is_shown_and_never_polled(live):
    async def scenario():
        account = live.broker.accounts[TOKEN_A]
        account["symbols"].append(market_symbol("US30", type_="CFD", base="USD", quote="USD", precision=1))
        cid = (await connect(live)).json()["connection"]["id"]
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await market_round(live)
        body = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert body["resolution"]["status"] == "ambiguous" and body["status"] == "unavailable"
        assert {c["symbol"] for c in body["resolution"]["candidates"]} == {"US30", "DJI30"} and "None was picked" in body["detail"]
        assert not (await q(live, "SELECT 1 FROM mtapi_market_feeds WHERE key LIKE 'live:%'"))
        account["symbols"] = [s for s in account["symbols"] if s["symbol"] not in ("US30", "DJI30")]
        await make_due(live)
        await market_round(live)
        missing = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert missing["resolution"]["status"] == "missing" and "DJI30" in missing["detail"]
        assert not [qs for p, qs in live.broker.queries if p == "/v1/quotes" and ("US30" in qs or "DJI30" in qs)]

    run_db(live, scenario)


@needs_database
def test_live_route_is_owner_scoped_flagged_and_rejects_other_instruments(live, monkeypatch):
    async def scenario():
        live.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        seed_us30(live.broker.accounts[TOKEN_B], name="US30", bid=50000.5, ask=50002.5)
        alice = (await connect(live, ALICE, TOKEN_A)).json()["connection"]["id"]
        bob = (await connect(live, BOB, TOKEN_B)).json()["connection"]["id"]
        for user, cid in ((ALICE, alice), (BOB, bob)):
            await live.http.get(f"{P}/{cid}/market/live", headers=auth(user))
        await market_round(live)
        for user, cid in ((ALICE, alice), (BOB, bob)):
            await live.http.get(f"{P}/{cid}/market/live", headers=auth(user))
        await make_due(live)
        await market_round(live)
        a = (await live.http.get(f"{P}/{alice}/market/live", headers=auth(ALICE))).json()
        b = (await live.http.get(f"{P}/{bob}/market/live", headers=auth(BOB))).json()
        assert a["resolution"]["symbol"]["symbol"] == "DJI30" and b["resolution"]["symbol"]["symbol"] == "US30"     # per account
        assert a["quote"]["bid"] == "44100.5" and b["quote"]["bid"] == "50000.5"
        res = await live.http.get(f"{P}/{alice}/market/live", headers={**auth(BOB), "X-User-Id": ALICE})
        assert res.status_code == 404 and res.json() == {"detail": "Connection not found"}
        assert (await live.http.get(f"{P}/{alice}/market/live")).status_code == 401
        assert (await live.http.get(f"{P}/{alice}/market/live?instrument=NAS100", headers=auth(ALICE))).status_code == 422   # US30 only, for now

        # Rollout flag off: the route is gone and existing live feeds stop being polled.
        monkeypatch.delenv("TALITRADE_MT_API_MARKET_LIVE_ENABLED")
        assert (await live.http.get(f"{P}/{alice}/market/live", headers=auth(ALICE))).status_code == 404
        await make_due(live)
        await market_round(live)
        (alive,), = await q(live, "SELECT COUNT(*) FROM mtapi_market_feeds WHERE key LIKE 'live:%' AND interest_until > NOW()")
        assert alive == 0
        mark = len(live.broker.queries)
        await make_due(live)
        await market_round(live)
        assert not [qs for p, qs in live.broker.queries[mark:] if p == "/v1/quotes" and "DJI30" in qs]

    run_db(live, scenario)


@needs_database
def test_market_closed_and_stale_live_states_are_reported(live):
    async def scenario():
        account = live.broker.accounts[TOKEN_A]
        seed_us30(account, session_open=False, provider_ms=now_minute_ms() - 3 * 3600 * 1000)
        cid = (await connect(live)).json()["connection"]["id"]
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await market_round(live)
        await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))
        await make_due(live)
        await market_round(live)
        closed = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert closed["status"] == "market_closed" and closed["resolution"]["symbol"]["session_open"] is False
        seed_us30(account)
        await make_due(live)
        await market_round(live)                                                          # session opens again
        async with live.db.begin() as conn:                                               # ...then nothing refreshes for a while
            await conn.execute(text("UPDATE mtapi_market_feeds SET last_success_at = NOW() - interval '25 seconds' WHERE key LIKE 'live:%'"))
        stale = (await live.http.get(f"{P}/{cid}/market/live", headers=auth(ALICE))).json()
        assert stale["status"] == "stale" and stale["quote"] is not None                 # last price kept, labelled delayed

    run_db(live, scenario)

