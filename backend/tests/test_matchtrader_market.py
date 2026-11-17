"""Match-Trader market data: client contract, instrument selection, acquisition, cache and
owner-scoped reads. Synthetic instruments and prices only; the broker is a scripted fake.

The database tests need TALITRADE_TEST_DATABASE_URL (a disposable database) and are skipped otherwise.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.matchtrader_api import client as api, config, market, ratelimit, worker  # noqa: E402
from tests.mt_fakes import TOKEN_A, TOKEN_B, FakeBroker, market_candle, market_quote, market_symbol  # noqa: E402
from tests.test_matchtrader_api_db import ALICE, BOB, P, auth, connect, ctx, pytestmark as needs_database, q, run_db, sync  # noqa: E402,F401

T0 = 1_794_600_000_000          # a fixed provider time (epoch ms); synthetic
M5 = 300_000


def run(coro):
    return asyncio.run(coro)


# ── client contract ──────────────────────────────────────────────────────────
def test_market_requests_are_get_only_allowlisted_and_encoded_as_documented():
    broker = FakeBroker()
    account = broker.accounts[TOKEN_A]
    account["symbols"] = [market_symbol("EURUSD"), market_symbol("USDJPY", base="USD", quote="JPY", precision=3)]
    account["quotes"] = {"EURUSD": market_quote("EURUSD", 1.08512, 1.08519, T0), "USDJPY": market_quote("USDJPY", 151.201, 151.214, T0)}
    account["candles"] = {("EURUSD", "M5"): [market_candle(T0 + i * M5, 1.08, 1.09, 1.07, 1.085) for i in range(5)]}

    async def scenario():
        async with api.MatchTraderApiClient(TOKEN_A, transport=broker.transport()) as client:
            assert [s.symbol for s in await client.symbols()] == ["EURUSD", "USDJPY"]
            quotes = await client.quotes(["EURUSD", "USDJPY", "NOT-OFFERED"])
            assert [q.symbol for q in quotes] == ["EURUSD", "USDJPY"]                 # unknown symbols are simply absent
            assert quotes[0].bid == Decimal("1.08512") and quotes[0].timestamp == T0   # exact decimals, provider time in ms
            candles = await client.candles("EURUSD", "M5", range_to=T0 + 10 * M5, limit=5000)
            assert [c.timestamp for c in candles] == [T0 + i * M5 for i in range(5)] and candles[0].tickCount == 10
            with pytest.raises(ValueError):
                await client.candles("EURUSD", "M2")                                  # not a documented interval
            with pytest.raises(ValueError):
                await client.quotes([])                                               # the symbol filter is required
            with pytest.raises(api.RequestRejected) as rejected:
                await client.candles("GBPUSD", "M5")
            assert rejected.value.status == 404

    run(scenario())
    assert {r[0] for r in broker.requests} == {"GET"}
    queries = dict((path, query) for path, query in broker.queries if path != "/v1/candles" or "EURUSD" in query)
    assert queries["/v1/quotes"] == "symbol=EURUSD&symbol=USDJPY&symbol=NOT-OFFERED"     # repeated keys, one request
    assert queries["/v1/candles"] == f"symbol=EURUSD&interval=M5&to={T0 + 10 * M5}&limit=750"   # limit clamped to the documented maximum
    assert {"/v1/symbols", "/v1/quotes", "/v1/candles"} <= api.ALLOWED_PATHS and all(p.startswith("/v1/") for p in api.ALLOWED_PATHS)
    assert ratelimit.ENDPOINT_LIMITS_PER_MINUTE["/v1/symbols"] == 10 and ratelimit.ENDPOINT_LIMITS_PER_MINUTE["/v1/quotes"] == 60
    assert ratelimit.ENDPOINT_LIMITS_PER_MINUTE["/v1/candles"] == 20


def test_missing_and_null_market_fields_are_tolerated_but_a_broken_candle_is_rejected():
    def serve(payloads):
        return httpx.MockTransport(lambda request: httpx.Response(200, json=payloads[request.url.path]))

    async def scenario():
        sparse = {"/v1/symbols": {"symbols": [{"symbol": "XAUUSD", "type": None, "terminationDate": None}]},
                  "/v1/quotes": {"quotes": [{"symbol": "XAUUSD", "bid": None, "ask": 4100.5}]},
                  "/v1/candles": {"candles": [{"timestamp": T0, "open": 1, "high": 2, "low": 0.5, "close": 1.5}], "interval": "M5", "symbol": "XAUUSD"}}
        async with api.MatchTraderApiClient(TOKEN_A, transport=serve(sparse)) as client:
            symbol = (await client.symbols())[0]
            assert symbol.alias is None and symbol.pricePrecision is None and symbol.sessionOpen is None
            quote = (await client.quotes(["XAUUSD"]))[0]
            assert quote.bid is None and quote.ask == Decimal("4100.5") and quote.timestamp is None
            assert (await client.candles("XAUUSD", "M5"))[0].tickCount is None
        broken = {"/v1/candles": {"candles": [{"timestamp": T0, "high": 2, "low": 0.5, "close": 1.5}]}, "/v1/symbols": {"instruments": []}}
        async with api.MatchTraderApiClient(TOKEN_A, transport=serve(broken)) as client:
            with pytest.raises(api.ContractError):
                await client.candles("XAUUSD", "M5")                                  # no open price: never guessed
            with pytest.raises(api.ContractError):
                await client.symbols()                                                # wrong envelope

    run(scenario())


# ── choosing instruments from what the account really offers ─────────────────
def rows(*symbols):
    return [{"symbol": s["symbol"], "alias": s["alias"], "type": s["type"], "base_currency": s["baseCurrency"], "quote_currency": s["quoteCurrency"],
             "session_open": s["sessionOpen"]} for s in symbols]


def test_watchlist_and_chart_symbol_come_only_from_offered_instruments_and_respect_aliases():
    offered = rows(market_symbol("AUDCAD", base="AUD", quote="CAD"), market_symbol("EURUSD.x", alias="EURUSD"),
                   market_symbol("GOLD", alias="XAUUSD", type_="CFD", base="XAU", quote="USD"), market_symbol("NZDUSD", base="NZD"),
                   market_symbol("EURUSDT", alias="EURUSDT", type_="CFD", base="EUR", quote="USDT"),
                   market_symbol("GER40", type_="CFD", base="GER40", quote="EUR"))
    watchlist = market.choose_watchlist(offered)
    assert watchlist == ["EURUSD.x", "GOLD", "AUDCAD", "NZDUSD"]                         # provider symbols; preferred first, then real FX pairs
    assert "VIX" not in watchlist and "BTCUSD" not in watchlist and "DJI30" not in watchlist   # nothing the account does not list
    assert market.choose_chart_symbol(offered)["symbol"] == "EURUSD.x"                   # EURUSD under the broker's own spelling
    without_euro = rows(market_symbol("GER40", type_="CFD", base="GER40", quote="EUR"), market_symbol("USDCHF", base="USD", quote="CHF"))
    assert market.choose_chart_symbol(without_euro)["symbol"] == "USDCHF"                 # another real FX pair, shown under its real name
    only_indices = rows(market_symbol("GER40", type_="CFD", base="GER40", quote="EUR"))
    assert market.choose_chart_symbol(only_indices) is None and market.choose_watchlist(only_indices) == []
    assert market.choose_watchlist([]) == [] and market.choose_chart_symbol([]) is None
    assert not market._matches({"symbol": "XEURUSD", "alias": "XEURUSD"}, "EURUSD")       # no substring matches


def test_timestamps_from_json_parse_with_any_fraction_length():
    assert market._parse_ts("2026-11-15T18:39:18.18941+00:00") == datetime(2026, 11, 15, 18, 39, 18, 189410, tzinfo=timezone.utc)
    assert market._parse_ts("2026-11-15T18:39:18+00:00").microsecond == 0 and market._parse_ts("2026-11-15T18:39:18.5Z").microsecond == 500000
    assert market._parse_ts("2026-11-15 10:39:18.123456-08:00").utcoffset() == timedelta(hours=-8)
    with pytest.raises(ValueError):
        market._parse_ts("yesterday")


def test_feed_status_separates_loading_closed_stale_denied_and_unavailable():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    ok = {"last_success_at": now - timedelta(seconds=10), "last_error_code": None}
    assert market.feed_status(None, has_data=False, now=now, poll_seconds=15) == ("loading", None)
    assert market.feed_status(ok, has_data=True, now=now, poll_seconds=15)[0] == "fresh"
    assert market.feed_status(ok, has_data=True, now=now, poll_seconds=15, session_open=False)[0] == "market_closed"   # old price, healthy feed
    old = {"last_success_at": now - timedelta(minutes=10), "last_error_code": None}
    assert market.feed_status(old, has_data=True, now=now, poll_seconds=15)[0] == "stale"
    assert market.feed_status(old, has_data=True, now=now, poll_seconds=15, session_open=False)[0] == "market_closed"
    denied = market.feed_status({"last_success_at": None, "last_error_code": "forbidden"}, has_data=False, now=now, poll_seconds=15)
    assert denied[0] == "permission_denied" and "not allowed to read market data" in denied[1]
    assert market.feed_status({"last_success_at": None, "last_error_code": "transient"}, has_data=False, now=now, poll_seconds=15)[0] == "unavailable"
    assert market.feed_status({"last_success_at": None, "last_error_code": "rate_limited"}, has_data=False, now=now, poll_seconds=15)[0] == "unavailable"
    # Held back by TaliTrade's own budget (account sync first): not reported as a broker limit or a failure.
    assert market.feed_status({"last_success_at": None, "last_error_code": "deferred"}, has_data=False, now=now, poll_seconds=15) == ("loading", None)
    assert market.feed_status({"last_success_at": now - timedelta(seconds=10), "last_error_code": "deferred"}, has_data=True, now=now, poll_seconds=15)[0] == "fresh"
    assert market._error_code(ratelimit.BudgetExhausted("/v1/candles", 12.5)) == "deferred"
    assert market._retry_in("deferred", ratelimit.BudgetExhausted("/v1/candles", 12.5), 60) == 13.5     # retried when the budget frees up, not a full interval later
    assert market._retry_in("deferred", ratelimit.BudgetExhausted("/v1/candles", 0.1), 60) == 5.0


def test_market_routes_do_not_exist_while_the_feature_is_off(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "true")
    monkeypatch.delenv("TALITRADE_MT_API_MARKET_DATA_ENABLED", raising=False)
    from app.main import app

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            for path in ("market/ticker", "market/candles"):
                assert (await http.get(f"{P}/1/{path}", headers=auth(ALICE))).status_code == 404
        assert config.service_status()["market_data"] is False

    run(scenario())


# ── acquisition, cache and reads against PostgreSQL ──────────────────────────
pytestmark_db = needs_database


def seed(account, *, session_open=True):
    account["symbols"] = [market_symbol("EURUSD", session_open=session_open), market_symbol("USDJPY", base="USD", quote="JPY", precision=3, session_open=session_open),
                          market_symbol("GOLD", alias="XAUUSD", type_="CFD", base="XAU", quote="USD", precision=2, session_open=session_open)]
    account["quotes"] = {"EURUSD": market_quote("EURUSD", 1.08512, 1.08519, T0), "USDJPY": market_quote("USDJPY", 151.201, 151.214, T0),
                         "GOLD": market_quote("GOLD", 4100.10, 4100.55, T0, alias="XAUUSD")}
    account["candles"] = {("EURUSD", "M5"): [market_candle(T0 - (9 - i) * M5, 1.0800 + i / 10000, 1.0810 + i / 10000, 1.0790 + i / 10000, 1.0805 + i / 10000, ticks=40 + i)
                                             for i in range(10)]}


@pytest.fixture
def mkt(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_MARKET_DATA_ENABLED", "true")
    seed(ctx.broker.accounts[TOKEN_A])
    return ctx


async def market_round(c):
    return await market.run_due_once(c.store, client_factory=c.factory, owner="w-market")


def calls(c, path):
    return [r for r in c.broker.requests if r[1] == path]


async def make_due(c):
    async with c.db.begin() as conn:
        await conn.execute(text("UPDATE mtapi_market_feeds SET next_due_at = NOW()"))


@needs_database
def test_ticker_reads_the_cache_registers_interest_and_many_tabs_share_one_poll(mkt):
    async def scenario():
        cid = (await connect(mkt)).json()["connection"]["id"]
        before = len(mkt.broker.requests)
        first = await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        body = first.json()
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        assert body["status"] == "loading" and body["items"] == [] and body["price_basis"] == "bid_ask" and body["updating"] == "auto"
        assert body["poll_interval_seconds"] == 15 and "_wake_worker" not in body
        assert len(mkt.broker.requests) == before                                  # a browser read never contacts the broker
        assert await market_round(mkt) == 1
        assert len(calls(mkt, "/v1/symbols")) == 1 and len(calls(mkt, "/v1/quotes")) == 1
        assert [q for p, q in mkt.broker.queries if p == "/v1/quotes"] == ["symbol=EURUSD&symbol=USDJPY&symbol=GOLD"]   # one batched request
        body = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()
        assert body["status"] == "fresh" and [i["symbol"] for i in body["items"]] == ["EURUSD", "USDJPY", "GOLD"]
        gold = body["items"][2]
        assert gold["alias"] == "XAUUSD" and gold["bid"] == "4100.1" and gold["ask"] == "4100.55" and gold["price_precision"] == 2
        assert gold["provider_time"] == "2026-11-13T20:00:00Z" and gold["fetched_at"] and gold["state"] == "fresh"      # provider time and fetch time are separate
        assert TOKEN_A not in str(body) and "700001" not in str(body) and "watchlist" not in str(body)

        # Ten tabs reading at once: the cache answers; the worker still polls once per interval.
        for _ in range(10):
            await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        assert await market_round(mkt) == 0 and len(calls(mkt, "/v1/quotes")) == 1  # not due again yet
        await make_due(mkt)
        assert await market_round(mkt) == 1 and len(calls(mkt, "/v1/quotes")) == 2
        (rows_,), = await q(mkt, "SELECT COUNT(*) FROM mtapi_market_feeds")
        assert rows_ == 2                                                          # 'symbols' and 'quotes': one row each, however many tabs

        # Nobody looking any more: polling stops, account sync does not.
        async with mkt.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_market_feeds SET interest_until = NOW() - interval '1 second', next_due_at = NOW()"))
        assert await market_round(mkt) == 0 and len(calls(mkt, "/v1/quotes")) == 2
        assert await market.seconds_until_next_due(mkt.store) is None
        assert (await sync(mkt, cid))["summary"] == "ok"
        stale = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()   # a new read revives interest
        assert stale["status"] in ("fresh", "stale") and (await market.seconds_until_next_due(mkt.store)) is not None

    run_db(mkt, scenario)


@needs_database
def test_market_data_is_owner_scoped_and_cached_per_account(mkt):
    async def scenario():
        mkt.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        other = mkt.broker.accounts[TOKEN_B]
        other["symbols"] = [market_symbol("EURUSD")]
        other["quotes"] = {"EURUSD": market_quote("EURUSD", 1.2, 1.3, T0 + 1000)}      # a different environment may quote differently
        other["candles"] = {("EURUSD", "M5"): [market_candle(T0, 1.2, 1.3, 1.1, 1.25)]}
        alice_cid = (await connect(mkt, ALICE, TOKEN_A)).json()["connection"]["id"]
        bob_cid = (await connect(mkt, BOB, TOKEN_B)).json()["connection"]["id"]
        for user, cid in ((ALICE, alice_cid), (BOB, bob_cid)):
            await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(user))
            await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(user))
        await market_round(mkt)
        for user, cid in ((ALICE, alice_cid), (BOB, bob_cid)):
            await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(user))         # instruments known now: the chart feed is registered
        await market_round(mkt)
        before = {t: await q(mkt, f"SELECT * FROM {t} ORDER BY 1, 2") for t in market.TABLES}
        broker_calls = len(mkt.broker.requests)
        for attacker, victim_cid in ((BOB, alice_cid), (ALICE, bob_cid)):
            for path in ("market/ticker", "market/candles", "market/candles?symbol=EURUSD&interval=M5", f"market/ticker?user_id={ALICE}"):
                res = await mkt.http.get(f"{P}/{victim_cid}/{path}", headers={**auth(attacker), "X-User-Id": ALICE})
                assert res.status_code == 404 and res.json() == {"detail": "Connection not found"}, path
            for headers in ({}, {"Authorization": "Bearer tti_not_a_session"}):
                assert (await mkt.http.get(f"{P}/{victim_cid}/market/ticker", headers=headers)).status_code == 401
        assert {t: await q(mkt, f"SELECT * FROM {t} ORDER BY 1, 2") for t in market.TABLES} == before   # no interest registered, nothing read or changed
        assert len(mkt.broker.requests) == broker_calls
        alice = (await mkt.http.get(f"{P}/{alice_cid}/market/ticker", headers=auth(ALICE))).json()
        bob = (await mkt.http.get(f"{P}/{bob_cid}/market/ticker", headers=auth(BOB))).json()
        assert alice["items"][0]["bid"] == "1.08512" and bob["items"][0]["bid"] == "1.2"          # same symbol, separate caches
        assert (await mkt.http.get(f"{P}/{bob_cid}/market/candles", headers=auth(BOB))).json()["candles"][0]["o"] == "1.2"
        tokens = {r[3] for r in mkt.broker.requests if r[1] in ("/v1/quotes", "/v1/candles", "/v1/symbols")}
        assert tokens == {TOKEN_A, TOKEN_B}                                              # each account's data is fetched with its own token

    run_db(mkt, scenario)


@needs_database
def test_candles_are_ordered_deduplicated_and_the_forming_candle_is_updated_in_place(mkt):
    async def scenario():
        cid = (await connect(mkt)).json()["connection"]["id"]
        first = (await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))).json()
        assert first["status"] == "loading" and first["symbol"] is None and first["candles"] == []
        await market_round(mkt)                                                          # instruments
        second = (await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))).json()
        assert second["symbol"]["symbol"] == "EURUSD" and second["status"] == "loading" and second["interval"] == "M5"   # default pair picked from the account's list
        account = mkt.broker.accounts[TOKEN_A]
        series = account["candles"][("EURUSD", "M5")]
        series.append(dict(series[3]))                                                   # the broker repeats a candle
        series.reverse()                                                                 # ...and answers out of order
        await market_round(mkt)
        window_limit = config.market_candle_window()
        assert [q_ for p, q_ in mkt.broker.queries if p == "/v1/candles"][-1].endswith(f"&limit={window_limit}")        # first load: the bounded window
        body = (await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))).json()
        times = [c["t"] for c in body["candles"]]
        assert times == sorted(set(times)) and len(times) == 10 and times[-1] == T0      # ascending, unique opening times in epoch ms
        assert body["candles"][0] == {"t": T0 - 9 * M5, "o": "1.08", "h": "1.081", "l": "1.079", "c": "1.0805", "ticks": 40}
        assert body["window"] == {"count": 10, "limit": window_limit, "is_complete_history": False, "first_open_time": "2026-11-13T19:15:00Z",
                                  "last_open_time": "2026-11-13T20:00:00Z"}
        assert body["price_side"] == "not_specified_by_provider" and body["status"] == "fresh" and body["available_intervals"] == ["M1", "M5", "H1"]

        # The forming candle moves, and a new one opens. Same key => overwritten; no duplicates.
        series[:] = sorted(series, key=lambda c: c["timestamp"])
        series[-1] = market_candle(T0, 1.0809, 1.0850, 1.0790, 1.0842, ticks=77)
        series.append(market_candle(T0 + M5, 1.0842, 1.0843, 1.0841, 1.0842, ticks=2))
        await make_due(mkt)
        await market_round(mkt)
        assert [q_ for p, q_ in mkt.broker.queries if p == "/v1/candles"][-1].endswith("&limit=3")                      # refresh: just the newest few
        body = (await mkt.http.get(f"{P}/{cid}/market/candles?symbol=EURUSD&interval=M5", headers=auth(ALICE))).json()
        assert len(body["candles"]) == 11 and body["candles"][-2] == {"t": T0, "o": "1.0809", "h": "1.085", "l": "1.079", "c": "1.0842", "ticks": 77}
        assert (await q(mkt, "SELECT COUNT(*), COUNT(DISTINCT open_time) FROM mtapi_market_candles"))[0] == (11, 11)

        # Other intervals and unknown instruments
        assert (await mkt.http.get(f"{P}/{cid}/market/candles?interval=M2", headers=auth(ALICE))).status_code == 422
        assert (await mkt.http.get(f"{P}/{cid}/market/candles?symbol=VIX", headers=auth(ALICE))).status_code == 404     # not offered by this account: never requested
        assert not [q_ for p, q_ in mkt.broker.queries if p == "/v1/candles" and "VIX" in q_]
        h1 = (await mkt.http.get(f"{P}/{cid}/market/candles?interval=H1", headers=auth(ALICE))).json()
        assert h1["interval"] == "H1" and h1["candles"] == [] and h1["status"] == "loading"                              # its own cache key; M5 data is not reused
        for interval in ("M1", "M5", "H1", "M1"):
            await mkt.http.get(f"{P}/{cid}/market/candles?interval={interval}", headers=auth(ALICE))
        (alive,), = await q(mkt, "SELECT COUNT(*) FROM mtapi_market_feeds WHERE key LIKE 'candles:%' AND interest_until > NOW()")
        assert alive <= market.MAX_CHART_FEEDS                                           # bounded, however many charts were opened

    run_db(mkt, scenario)


@needs_database
def test_closed_market_permission_denied_throttling_and_revoked_token_are_reported_truthfully(mkt, caplog):
    async def scenario():
        account = mkt.broker.accounts[TOKEN_A]
        seed(account, session_open=False)                                               # weekend: session closed, last prices are old
        cid = (await connect(mkt)).json()["connection"]["id"]
        await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))
        await market_round(mkt)
        await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))
        await market_round(mkt)
        closed = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()
        assert closed["status"] == "market_closed" and all(i["state"] == "market_closed" and i["session_open"] is False for i in closed["items"])
        assert closed["items"][0]["provider_time"] == "2026-11-13T20:00:00Z" and closed["last_error"] is None          # old provider time, healthy feed
        chart = (await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))).json()
        assert chart["status"] == "market_closed" and chart["symbol"]["session_open"] is False and len(chart["candles"]) == 10

        # The token may read the account but not market data.
        mkt.broker.fail["/v1/quotes"] = (403, {})
        mkt.broker.fail["/v1/candles"] = (403, {})
        await make_due(mkt)
        await market_round(mkt)
        denied = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()
        assert denied["status"] == "permission_denied" and "not allowed to read market data" in denied["detail"] and denied["last_error"] == "forbidden"
        assert (await mkt.http.get(f"{P}/{cid}/market/candles", headers=auth(ALICE))).json()["status"] == "permission_denied"
        (wait,), = await q(mkt, "SELECT EXTRACT(EPOCH FROM next_due_at - NOW()) FROM mtapi_market_feeds WHERE key = 'quotes'")
        assert float(wait) > 3000                                                       # not retried every few seconds
        assert (await sync(mkt, cid))["summary"] == "ok"                                 # account sync is unaffected
        assert (await mkt.store.get_connection(cid))["status"] == "active"

        # Throttled: backs off for at least Retry-After, no retry storm, account sync still fine.
        mkt.broker.fail["/v1/quotes"] = (429, {"Retry-After": "40", "X-RateLimit-Remaining-Endpoint": "0", "X-RateLimit-Remaining-Global": "20"})
        mkt.broker.fail.pop("/v1/candles")
        await make_due(mkt)
        quotes_before = len(calls(mkt, "/v1/quotes"))
        await market_round(mkt)
        (wait, error), = await q(mkt, "SELECT EXTRACT(EPOCH FROM next_due_at - NOW()), last_error_code FROM mtapi_market_feeds WHERE key = 'quotes'")
        assert error == "rate_limited" and float(wait) >= 39
        for _ in range(3):
            await market_round(mkt)
        assert len(calls(mkt, "/v1/quotes")) == quotes_before + 1                        # exactly one attempt
        throttled = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()
        assert throttled["items"] and throttled["last_error"] == "rate_limited"         # last good prices are still shown
        assert (await sync(mkt, cid, live=True, history=False))["summary"] == "ok"

        # The instrument disappears at the broker: that chart feed is dropped instead of being retried forever.
        mkt.broker.fail.clear()
        ratelimit._budgets.clear()
        account["symbols"] = [s for s in account["symbols"] if s["symbol"] != "EURUSD"]
        async with mkt.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_market_feeds SET next_due_at = NOW() WHERE key LIKE 'candles:%'"))
        await market_round(mkt)
        (error, alive), = await q(mkt, "SELECT last_error_code, interest_until > NOW() FROM mtapi_market_feeds WHERE key LIKE 'candles:%'")
        assert error == "symbol_not_found" and alive is False

        # Revoked token: the connection needs attention; nothing is retried.
        del mkt.broker.accounts[TOKEN_A]
        await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        await make_due(mkt)
        await market_round(mkt)
        assert (await mkt.store.get_connection(cid))["status"] == "needs_reauth"
        total = len(mkt.broker.requests)
        await make_due(mkt)
        assert await market_round(mkt) == 0 and len(mkt.broker.requests) == total
        final = (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()
        assert final["status"] == "stale" and "not active" in final["detail"]

    run_db(mkt, scenario)
    assert TOKEN_A not in caplog.text


@needs_database
def test_market_polling_cannot_starve_account_sync_or_bypass_the_rate_budget(mkt, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_GLOBAL_PER_MINUTE", "8")                       # a tight account-wide budget

    async def scenario():
        cid = (await connect(mkt)).json()["connection"]["id"]
        ratelimit._budgets.clear()
        await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))
        await market_round(mkt)                                                          # symbols + quotes: 2 low-priority calls
        market_calls = lambda: len([r for r in mkt.broker.requests if r[1] in ("/v1/symbols", "/v1/quotes", "/v1/candles")])   # noqa: E731
        assert market_calls() == 2
        for _ in range(6):                                                               # the market keeps asking...
            await make_due(mkt)
            await market_round(mkt)
        assert market_calls() <= 4                                                       # ...but only half the budget is ever open to it
        (error, wait), = await q(mkt, "SELECT last_error_code, EXTRACT(EPOCH FROM next_due_at - NOW()) FROM mtapi_market_feeds WHERE key = 'quotes'")
        assert error == "deferred" and 4 <= float(wait) <= 62                            # TaliTrade's own budget said wait: retried when it frees up
        assert (await mkt.http.get(f"{P}/{cid}/market/ticker", headers=auth(ALICE))).json()["last_error"] in (None, "deferred")
        live = await sync(mkt, cid, live=True, history=False)                            # account sync still gets through
        assert live == {"summary": "ok", "positions": "ok", "pending_orders": "ok"}
        assert len(mkt.broker.requests) - 7 <= 8                                         # 7 probe calls at connect; then never above the cap

        # The worker round does account work first and market work last.
        ratelimit._budgets.clear()
        async with mkt.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET next_live_sync_at = NOW()"))
        await make_due(mkt)
        mark = len(mkt.broker.requests)
        await worker.run_due_once(mkt.store, client_factory=mkt.factory)
        order = [r[1] for r in mkt.broker.requests[mark:]]
        market_positions = [i for i, path in enumerate(order) if path in ("/v1/symbols", "/v1/quotes")]
        assert "/v1/account-summary" in order and all(i > order.index("/v1/account-summary") for i in market_positions)
        assert len(market_positions) <= 1                                                # account sync and history used the budget first; the market waited
        (lease,), = await q(mkt, "SELECT lease_owner FROM mtapi_connections")
        assert lease is None                                                             # the market pass released the shared lease

    run_db(mkt, scenario)
