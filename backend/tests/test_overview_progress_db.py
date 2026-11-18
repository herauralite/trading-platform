"""Overview progress through the real route and PostgreSQL: server-side totals over every stored
entry (not a page), partial closes and re-sent duplicates, the dashboard day in an explicit time
zone, the saved setup profile, and ownership. Synthetic broker; disposable test database."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.services.account_setup import routes as setup_routes
from app.services.account_setup.store import SetupStore
from app.services.matchtrader_api import progress as pg
from tests.mt_fakes import TOKEN_A, TOKEN_B, closed
from tests.test_history_import import rounds
from tests.test_matchtrader_api_db import ALICE, BOB, P, auth, connect, ctx, pytestmark, q, run_db  # noqa: F401

TZ = "America/Los_Angeles"
EVAL = {"status": "saved", "type": "evaluation", "size": {"amount": "100000", "currency": "USD"},
        "evaluation": {"phaseCount": 2, "currentPhase": 1, "phaseTarget": {"value": "8", "unit": "percent", "base": "starting"}},
        "firmRules": {"overall": {"value": "10", "unit": "percent", "base": "starting"}, "overallMode": "static", "overallRef": "equity"},
        "personal": {"style": "Day trading"}, "done": {"basics": True, "phase": True}}


def setup_store(c, monkeypatch):
    store = SetupStore(lambda: c.db)
    monkeypatch.setattr(setup_routes, "get_store", lambda: store)
    return store


async def fresh_setup_table(c, store):
    async with c.db.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS account_setup_profiles"))
    await store.ensure_schema()


def test_progress_totals_today_from_every_stored_entry_with_the_saved_profile(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
    store = setup_store(ctx, monkeypatch)

    async def scenario():
        await fresh_setup_table(ctx, store)
        now = datetime.now(timezone.utc)
        _, d0, _ = pg.local_day(now, pg.zone(TZ))
        if now - d0 < timedelta(minutes=5):
            pytest.skip("too close to local midnight to place today's trades")
        t0 = int(d0.timestamp() * 1000)
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = int(time.time() * 1000) - 5 * 86_400_000
        acct["summary"].update(balance=104000, equity=103500, profit=-500)
        acct["closed"] = [closed("a1", "PX", t0 + 60_000, net="120.25"),            # a position closed in two parts today
                          closed("a2", "PX", t0 + 120_000, net="-20.25"),
                          closed("b1", "PY", t0 + 180_000, net="-310"),
                          closed("y1", "PZ", t0 - 60_000, net="999")]               # yesterday 23:59 local: not today
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx, 3)
        acct["closed"].append(closed("a1", "PX", t0 + 60_000, net="120.25"))         # the broker sends a1 again
        await rounds(ctx, 1)
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_closed_positions"))[0][0] == 4
        put = await ctx.http.put(f"/account-setup/connections/{cid}", headers=auth(ALICE), json={"base_version": 0, "save_id": "save-eval-0001", "profile": EVAL})
        assert put.status_code == 200, put.text

        res = await ctx.http.get(f"{P}/{cid}/progress", params={"tz": TZ}, headers=auth(ALICE))
        assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
        body = res.json()
        d = body["daily_pnl"]
        assert d["value"] == "-210.00" and d["closed_entries"] == 3 and d["positions"] == 2 and d["status"] == "verified", d   # 120.25 - 20.25 - 310
        assert d["time_zone"] == TZ and d["open_pnl"] == "-500.00" and body["currency"] == "USD"
        r = {x["id"]: x for x in body["rows"]}
        assert r["phase_target"]["current"] == "4000.00" and r["phase_target"]["target"] == "8000.00" and r["phase_target"]["ratio_percent"] == "50.0"
        assert r["overall_drawdown"]["used"] == "0.00" and r["overall_drawdown"]["remaining"] == "13500.00"   # equity 103,500 vs floor 90,000
        assert body["overview"]["label"] == "Phase 1 profit target" and body["labels"] == [{"title": "Trading style", "value": "Day trading"}]
        tokyo = (await ctx.http.get(f"{P}/{cid}/progress", params={"tz": "Asia/Tokyo"}, headers=auth(ALICE))).json()["daily_pnl"]
        assert tokyo["time_zone"] == "Asia/Tokyo" and tokyo["day_start"] != d["day_start"]
        assert (await ctx.http.get(f"{P}/{cid}/progress", params={"tz": "Mars/Base"}, headers=auth(ALICE))).status_code == 422
    run_db(ctx, scenario)


def test_progress_belongs_to_the_signed_in_owner_only(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
    store = setup_store(ctx, monkeypatch)

    async def scenario():
        await fresh_setup_table(ctx, store)
        alice = (await connect(ctx)).json()["connection"]["id"]
        ctx.broker.add_account(TOKEN_B, login="7009001", platform_url="https://mtr.example-two.test")
        bob = (await connect(ctx, user=BOB, token=TOKEN_B)).json()["connection"]["id"]
        await rounds(ctx, 1)
        assert (await ctx.http.get(f"{P}/{alice}/progress", headers=auth(BOB))).status_code == 404
        assert (await ctx.http.get(f"{P}/{bob}/progress", headers=auth(ALICE))).status_code == 404
        assert (await ctx.http.get(f"{P}/{alice}/progress", params={"user_id": BOB}, headers=auth(ALICE))).json()["connection_id"] == alice
        assert (await ctx.http.get(f"{P}/{alice}/progress")).status_code == 401
        assert (await ctx.http.get(f"{P}/999999/progress", headers=auth(ALICE))).status_code == 404
    run_db(ctx, scenario)


def test_daily_pnl_still_works_with_account_setup_off(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "false")

    async def scenario():
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx, 1)
        body = (await ctx.http.get(f"{P}/{cid}/progress", params={"tz": "UTC"}, headers=auth(ALICE))).json()
        assert body["setup"] == "disabled" and body["rows"] == [] and body["overview"] is None
        assert body["daily_pnl"]["basis"] == "realized_net"
    run_db(ctx, scenario)


def test_master_cash_and_trailing_peak_through_the_real_queries(ctx, monkeypatch):
    """Funding deposit, a payout, a full history import: the trailing peak is rebuilt in SQL from
    trades in time order, the payout is not a drawdown, and payout progress waits for the cycle start."""
    from tests.test_history_import import until_settled
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
    store = setup_store(ctx, monkeypatch)
    DAY = 86_400_000

    async def scenario():
        await fresh_setup_table(ctx, store)
        now = int(time.time() * 1000)
        created = now - 60 * DAY
        acct = ctx.broker.accounts[TOKEN_A]
        acct["account"]["createTime"] = created
        acct["closed"] = [closed("t1", "P1", now - 50 * DAY, net="1500"), closed("t2", "P2", now - 45 * DAY, net="1500"),   # peak +3,000
                          closed("t3", "P3", now - 40 * DAY, net="-800"),                                                     # 2,200
                          closed("t4", "P4", now - 10 * DAY, net="600")]                                                      # 2,800 after the payout
        acct["ledgers"] = [{"id": "F", "comment": "", "amount": 50000, "timestamp": created + 60_000, "type": "DEPOSIT"},
                           {"id": "W", "comment": "", "amount": -2200, "timestamp": now - 30 * DAY, "type": "WITHDRAWAL"}]
        acct["summary"].update(balance=50600, equity=50600, profit=0)                  # 50,000 + 2,800 - 2,200
        cid = (await connect(ctx)).json()["connection"]["id"]
        await rounds(ctx, 2)
        await ctx.http.post(f"{P}/{cid}/history-import", headers=auth(ALICE))
        assert (await until_settled(ctx, cid))["state"] == "imported"
        profile = {"status": "saved", "type": "master", "size": {"amount": "50000", "currency": "USD"},
                   "master": {"goal": {"id": "payout"}, "payoutTarget": {"value": "1000", "unit": "amount", "base": "starting"}, "profitSplit": "80"},
                   "firmRules": {"overall": {"value": "6", "unit": "percent", "base": "high_water"}, "overallMode": "trailing", "overallRef": "balance"},
                   "done": {"basics": True}}
        put = await ctx.http.put(f"/account-setup/connections/{cid}", headers=auth(ALICE), json={"base_version": 0, "save_id": "save-master-01", "profile": profile})
        assert put.status_code == 200, put.text
        body = (await ctx.http.get(f"{P}/{cid}/progress", params={"tz": "UTC"}, headers=auth(ALICE))).json()
        r = {x["id"]: x for x in body["rows"]}
        dd = r["overall_drawdown"]
        # cash-adjusted: start 50,000; peak 53,000; current 50,600 + 2,200 = 52,800; used 200; limit 6% of 53,000 = 3,180
        assert dd["measurable"] and dd["reference"]["amount"] == "53000.00" and dd["used"] == "200.00" and dd["limit"] == "3180.00", dd
        assert dd["status_label"] == "Within limit" and dd["cash_excluded"] == "-2200.00"
        pay = r["payout_target"]
        assert not pay["measurable"] and pay["action"]["flow"] == "cycle" and pay["action"]["withdrawal"] == "2200.00"
        profile["master"]["cycleStart"] = {"amount": "50000", "after": pay["action"]["after"]}
        put = await ctx.http.put(f"/account-setup/connections/{cid}", headers=auth(ALICE), json={"base_version": 1, "save_id": "save-master-02", "profile": profile})
        assert put.status_code == 200, put.text
        pay = {x["id"]: x for x in (await ctx.http.get(f"{P}/{cid}/progress", params={"tz": "UTC"}, headers=auth(ALICE))).json()["rows"]}["payout_target"]
        assert pay["current"] == "600.00" and pay["your_share"] == "480.00" and pay["status_label"] == "In progress", pay   # 50,600 - 50,000
    run_db(ctx, scenario)
