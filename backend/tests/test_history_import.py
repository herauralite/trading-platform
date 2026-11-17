"""One-tap trade-history import: extends the connect-time import, never overlaps it, reports
only committed results, and stays with the account's owner. Synthetic trades; scripted broker.
Needs TALITRADE_TEST_DATABASE_URL (a disposable database)."""
import asyncio
import time

import httpx
from sqlalchemy import text

from app.services.matchtrader_api import history_import, market, ratelimit, worker
from tests.mt_fakes import TOKEN_A, closed
from tests.test_matchtrader_api_db import ALICE, BOB, P, auth, connect, ctx, pytestmark, q, run_db  # noqa: F401

DAY = 86_400_000


async def rounds(c, n=1):
    """Let the worker run ``n`` times, as if it were due each time (the browser is not involved)."""
    for _ in range(n):
        async with c.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET next_history_sync_at = NOW(), next_live_sync_at = NOW()"))
        ratelimit._budgets.clear()
        await worker.run_due_once(c.store, client_factory=c.factory)


async def until_settled(c, cid, user=ALICE, limit=40):
    for _ in range(limit):
        body = (await c.http.get(f"{P}/{cid}/history-import", headers=auth(user))).json()
        if body["state"] not in ("importing", "not_requested"):
            return body
        await rounds(c)
    raise AssertionError("import did not settle")


def trading_periods(now):
    """Recent trades, older trades and a 90-day quiet gap between them."""
    rows = [closed(f"r{i}", f"R{i}", now - 3_600_000 - i * 9 * 3_600_000, net="4") for i in range(12)]              # last ~5 days
    rows += [closed(f"m{i}", f"M{i}", now - 10 * DAY - i * 3 * 3_600_000, net="-2") for i in range(300)]          # 10-48 days ago
    rows += [closed(f"o{i}", f"O{i}", now - 140 * DAY - i * 5 * 3_600_000, net="7") for i in range(260)]          # 140-194 days ago (quiet 48-140)
    return rows


def test_one_tap_imports_everything_back_to_account_creation_without_overlap(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = now - 200 * DAY
        acct["closed"] = trading_periods(now)
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx, 2)
        before = (await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        assert before["state"] == "not_requested" and before["closed_trades"] == 12          # only the connect-time 7 days so far
        # double tap: two requests at once
        a, b = await asyncio.gather(ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE)),
                                    ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE)))
        assert a.status_code == b.status_code == 200 and a.json()["state"] == "importing"
        ranges = await q(ctx, """SELECT range_from, range_to, status FROM mtapi_history_intervals
                                 WHERE resource = 'closed_positions' AND kind = 'backfill' ORDER BY range_from""")
        assert len(ranges) == 2, ranges                                                           # the 7-day range + ONE extension
        (older_from, older_to, _), (seven_from, _, seven_status) = ranges
        assert seven_status == "done" and older_to == seven_from + 1                              # extends it, 1 ms overlap, no overlap job
        assert abs(older_from + 1 - (now - 200 * DAY)) < 5                                        # back to the broker-reported creation time
        assert (await q(ctx, "SELECT attempts FROM mtapi_history_imports"))[0][0] == 1
        done = await until_settled(ctx, cid)
        assert done["state"] == "imported" and done["closed_trades"] == len(acct["closed"]), done
        assert done["earliest_boundary"] == "account_created_at" and done["checked_from"][:10] == done["account_created_at"][:10]
        assert done["queued_ranges"] == 0 and done["gaps"] == [] and done["retention_known"] is False
        (total, distinct), = await q(ctx, "SELECT COUNT(*), COUNT(DISTINCT uid) FROM mtapi_closed_positions")
        assert total == distinct == len(acct["closed"])                                           # each trade exactly once
        # pagination: the 10-194 day range held more than one page (500 rows) and was bisected
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_intervals WHERE status = 'split'"))[0][0] >= 1
        again = (await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        assert again["state"] == "imported" and (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_intervals WHERE status = 'queued' AND kind = 'backfill'"))[0][0] == 0
    run_db(ctx, scenario)


def test_progress_is_committed_results_only_and_moves_backwards_in_time(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")
    monkeypatch.setenv("TALITRADE_MT_API_BACKFILL_INTERVALS_PER_CYCLE", "0")       # one range per pass: progress is visible in steps

    async def scenario():
        now = int(time.time() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = now - 200 * DAY
        acct["closed"] = trading_periods(now)
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx)
        first = (await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        seen = [first]
        for _ in range(12):
            await rounds(ctx)
            seen.append((await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(ALICE))).json())
            if seen[-1]["state"] == "imported":
                break
        counts = [s["closed_trades"] for s in seen]
        froms = [s["checked_from"] for s in seen if s["checked_from"]]
        assert counts == sorted(counts) and counts[-1] == len(acct["closed"])                     # never goes down, ends at the true total
        assert froms == sorted(froms, reverse=True)                                               # the checked range only grows backwards
        assert any(s["state"] == "importing" and 0 < s["closed_trades"] < len(acct["closed"]) for s in seen)
        assert all("percent" not in str(s) for s in seen)
    run_db(ctx, scenario)


def test_rate_limits_pause_and_failures_interrupt_then_retry_resumes_without_duplicates(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = now - 200 * DAY
        acct["closed"] = trading_periods(now)
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx)
        await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))
        ctx.broker.fail["/v1/closed-positions"] = (429, {"Retry-After": "1"})
        await rounds(ctx)
        paused = (await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        assert paused["state"] == "importing" and paused["reason"] == "rate_limited"             # waiting, not failed
        ctx.broker.fail.pop("/v1/closed-positions")
        await rounds(ctx)                                                                          # partial progress
        partial = (await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        ctx.broker.fail["/v1/closed-positions"] = (503, {})
        await rounds(ctx)
        stuck = (await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(ALICE))).json()
        if stuck["state"] != "imported":
            assert stuck["state"] == "interrupted" and stuck["reason"] == "transient" and stuck["closed_trades"] >= partial["closed_trades"]
        ctx.broker.fail.pop("/v1/closed-positions")
        retry = (await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE), json={"retry": True})).json()
        assert retry["attempts"] == 2
        done = await until_settled(ctx, cid)
        assert done["state"] == "imported" and done["closed_trades"] == len(acct["closed"])
        (total, distinct), = await q(ctx, "SELECT COUNT(*), COUNT(DISTINCT uid) FROM mtapi_closed_positions")
        assert total == distinct
    run_db(ctx, scenario)


def test_an_empty_account_is_reported_with_the_dates_checked(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        ctx.broker.accounts[TOKEN_A]["account"]["createTime"] = now - 90 * DAY
        cid = (await connect(ctx)).json()["connection"]["id"]
        await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))
        done = await until_settled(ctx, cid)
        assert done["state"] == "empty" and done["closed_trades"] == 0
        assert done["checked_from"][:10] == done["account_created_at"][:10] and done["checked_to"]
    run_db(ctx, scenario)


def test_without_a_creation_time_the_floor_is_used_and_said(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = None
        acct["closed"] = [closed("x1", "X1", now - 900 * DAY, net="1"), closed("x2", "X2", now - DAY, net="1")]
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx)
        await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))
        done = await until_settled(ctx, cid)
        assert done["earliest_boundary"] == "not_reported" and done["checked_from"].startswith("2000-01-01")
        assert done["state"] == "imported" and done["closed_trades"] == 2
    run_db(ctx, scenario)


def test_a_broker_window_limit_is_split_by_time_not_taken_as_empty(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = now - 200 * DAY
        acct["closed"] = trading_periods(now)
        handler = ctx.broker.handler

        def limited(request):                     # this broker refuses ranges longer than 30 days
            if request.url.path == "/v1/closed-positions" and int(request.url.params["to"]) - int(request.url.params["from"]) > 30 * DAY:
                return httpx.Response(400, json={"type": "error://trader-api/validation"})
            return handler(request)
        ctx.broker.handler = limited
        cid = (await connect(ctx)).json()["connection"]["id"]
        await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))
        done = await until_settled(ctx, cid, limit=80)
        assert done["state"] == "imported" and done["window_limited"] is True and done["closed_trades"] == len(acct["closed"])
    run_db(ctx, scenario)


def test_another_trader_cannot_start_or_read_the_import(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        for res in (await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(BOB)), await ctx.http.get(f"{P}/{cid}/history-import", headers=auth(BOB)),
                    await ctx.http.post(f"{P}/999999/history-import", headers=auth(ALICE))):
            assert res.status_code == 404
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_imports"))[0][0] == 0
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_intervals WHERE kind = 'backfill' AND range_from < :t", t=int(time.time() * 1000) - 8 * DAY))[0][0] == 0
        assert (await ctx.http.post(f"{P}/{cid}/history-import")).status_code == 401
        assert (await ctx.http.post(f"{P}/{cid}/history-import", headers={"Authorization": "Bearer tti_x"})).status_code == 401
    run_db(ctx, scenario)


def test_the_chart_prefers_the_instrument_this_account_trades(ctx):
    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        async with ctx.db.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS mtapi_market_symbols (connection_id INTEGER, symbol TEXT)"))
        offered = [{"symbol": "EURUSD"}, {"symbol": "XAUUSD"}]
        assert await market._recently_traded_symbols(ctx.store, cid, ALICE) == []
        async with ctx.db.begin() as conn:
            await conn.execute(text("""INSERT INTO mtapi_closed_positions (connection_id, uid, position_id, symbol, side, volume, open_price, close_price, net_profit, open_time, close_time, raw)
                                       VALUES (:i, 'u1', 'p1', 'XAUUSD', 'BUY', 1, 1, 1, 1, NOW(), NOW(), '{}')"""), {"i": cid})
        recent = await market._recently_traded_symbols(ctx.store, cid, ALICE)
        assert recent == ["XAUUSD"] and next(s for n in recent for s in offered if s["symbol"] == n)["symbol"] == "XAUUSD"
        assert await market._recently_traded_symbols(ctx.store, cid, BOB) == []                 # another trader's trades never count
    run_db(ctx, scenario)
