"""Account setup profiles: storage, ownership, conflicts and the feature flag, against a real
PostgreSQL. Synthetic connections only (rows inserted directly; no broker, no network).
Runs only when TALITRADE_TEST_DATABASE_URL names a disposable database ("test"/"disposable")."""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core.auth_session import create_session_token  # noqa: E402
from app.main import app  # noqa: E402
from app.services.account_setup import routes as setup_routes, schemas  # noqa: E402
from app.services.account_setup.store import DROP_ORDER as SETUP_TABLES, SetupStore  # noqa: E402
from app.services.matchtrader_api import config as mt_config  # noqa: E402
from app.services.matchtrader_api.store import DROP_ORDER as MT_TABLES, SqlStore  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")

ALICE, BOB = "1001", "2002"
S = "/account-setup"
MASTER = {"status": "saved", "type": "master", "size": {"amount": "50000", "currency": "USD"},
          "master": {"goal": {"id": "payout"}, "payoutTarget": {"value": "2500", "unit": "amount", "base": "starting"}},
          "done": {"basics": True}}


def auth(user):
    return {"Authorization": f"Bearer {create_session_token(user)}"}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "true")
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
    c = SimpleNamespace(db=None, http=None, store=None)
    monkeypatch.setattr(setup_routes, "get_store", lambda: c.store)
    c.monkeypatch = monkeypatch
    return c


def run_db(c, scenario):
    async def main():
        c.db = create_async_engine(URL, poolclass=NullPool)
        async with c.db.begin() as conn:
            for table in SETUP_TABLES + MT_TABLES:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        await SqlStore(lambda: c.db).ensure_schema()
        c.store = SetupStore(lambda: c.db)
        await c.store.ensure_schema()
        await c.store.ensure_schema()   # idempotent
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
                c.http = http
                await scenario()
        finally:
            await c.db.dispose()
    asyncio.run(main())


async def q(c, sql, **params):
    async with c.db.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.all() if result.returns_rows else []


async def account(c, user=ALICE, login="700001", host="mtr.broker-one.example", account_type="REAL", currency="USD", synced=False):
    (cid,), = await q(c, """INSERT INTO mtapi_connections (user_id, platform_host, external_login, broker_name, account_currency, account_type)
                            VALUES (:u, :h, :l, 'FundingPips', :cur, :t) RETURNING id""", u=user, h=host, l=login, cur=currency, t=account_type)
    if synced:
        await q(c, "INSERT INTO mtapi_account_summary (connection_id, balance, equity, fetched_at) VALUES (:i, 49820.00, 49820.00, NOW())", i=cid)
        await q(c, "INSERT INTO mtapi_sync_state (connection_id, resource, last_success_at) VALUES (:i, 'summary', NOW())", i=cid)
    return cid


async def save(c, cid, profile, base, save_id="save-0001", user=ALICE):
    return await c.http.put(f"{S}/connections/{cid}", headers=auth(user), json={"base_version": base, "save_id": save_id, "profile": profile})


def test_flag_off_answers_404_and_keeps_saved_profiles(ctx):
    async def scenario():
        cid = await account(ctx)
        assert (await save(ctx, cid, MASTER, 0)).status_code == 200
        ctx.monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "false")
        assert mt_config.service_status(ALICE)["account_setup"] is False
        for res in (await ctx.http.get(f"{S}/profiles", headers=auth(ALICE)), await ctx.http.get(f"{S}/connections/{cid}", headers=auth(ALICE)),
                    await save(ctx, cid, MASTER, 1, "save-0002")):
            assert res.status_code == 404
        assert (await q(ctx, "SELECT version FROM account_setup_profiles"))[0][0] == 1      # untouched while off
        ctx.monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
        assert mt_config.service_status(ALICE)["account_setup"] is True
        body = (await ctx.http.get(f"{S}/connections/{cid}", headers=auth(ALICE))).json()
        assert body["version"] == 1 and body["profile"]["master"]["goal"]["id"] == "payout"
    run_db(ctx, scenario)


def test_flag_needs_the_match_trader_integration(monkeypatch):
    monkeypatch.setenv("TALITRADE_ACCOUNT_SETUP_ENABLED", "true")
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "false")
    from app.services.account_setup import config
    assert config.enabled() is False


def test_detected_facts_are_broker_reported_only(ctx):
    async def scenario():
        pending = await account(ctx, login="700001")
        body = (await ctx.http.get(f"{S}/connections/{pending}", headers=auth(ALICE))).json()
        assert body["version"] == 0 and body["profile"] is None
        d = body["detected"]
        assert d["broker"] == "FundingPips" and d["currency"] == "USD" and d["reported_account_type"] == "REAL"
        assert d["balance"] is None and d["observed_at"] is None        # nothing synced yet: unknown, not zero
        assert "700001" not in str(body) and d["masked_login"].endswith("0001")
        synced = await account(ctx, login="700002", synced=True)
        d = (await ctx.http.get(f"{S}/connections/{synced}", headers=auth(ALICE))).json()["detected"]
        assert d["balance"] == "49820.00" and d["observed_at"]
        assert {"starting_size", "evaluation_or_funded", "evaluation_phase"} <= set(d["not_supplied"])
        assert not any(k in d for k in ("starting_size", "account_size", "phase"))   # never filled from the balance
    run_db(ctx, scenario)


def test_save_resume_from_a_fresh_session_and_list(ctx):
    async def scenario():
        cid = await account(ctx)
        draft = {**MASTER, "status": "in_progress", "resume": {"flow": "payout", "step": "firmMin"},
                 "draft": {"firmMin": {"raw": "1", "unit": "percent", "view": "percent"}}}
        res = await save(ctx, cid, draft, 0)
        assert res.status_code == 200 and res.json()["version"] == 1 and res.headers["cache-control"] == "no-store"
        # another device: a new session token for the same Telegram user
        other_device = {"Authorization": f"Bearer {create_session_token(ALICE)}"}
        body = (await ctx.http.get(f"{S}/connections/{cid}", headers=other_device)).json()
        assert body["profile"]["resume"] == {"flow": "payout", "step": "firmMin"}
        assert body["profile"]["draft"]["firmMin"]["raw"] == "1" and body["profile"]["size"] == {"amount": "50000", "currency": "USD"}
        listing = (await ctx.http.get(f"{S}/profiles", headers=other_device)).json()["profiles"]
        assert [p["connection_id"] for p in listing] == [cid] and listing[0]["version"] == 1
        fresh = await account(ctx, login="700009")       # an account linked before setup existed: listed, no profile
        listing = (await ctx.http.get(f"{S}/profiles", headers=auth(ALICE))).json()["profiles"]
        assert {p["connection_id"]: p["profile"] is None for p in listing} == {cid: False, fresh: True}
    run_db(ctx, scenario)


def test_older_tab_cannot_overwrite_newer_answers_and_retries_are_safe(ctx):
    async def scenario():
        cid = await account(ctx)
        assert (await save(ctx, cid, MASTER, 0, "tab-a-0001")).json()["version"] == 1
        newer = {**MASTER, "size": {"amount": "100000", "currency": "USD"}}
        assert (await save(ctx, cid, newer, 1, "tab-b-0001")).json()["version"] == 2
        stale = await save(ctx, cid, {**MASTER, "size": {"amount": "5000", "currency": "USD"}}, 1, "tab-a-0002")
        assert stale.status_code == 409 and stale.json()["version"] == 2 and stale.json()["profile"]["size"]["amount"] == "100000"
        retry = await save(ctx, cid, newer, 1, "tab-b-0001")          # same save again after a lost response
        assert retry.status_code == 200 and retry.json()["replayed"] is True and retry.json()["version"] == 2
        (version, amount), = await q(ctx, "SELECT version, data->'size'->>'amount' FROM account_setup_profiles")
        assert (version, amount) == (2, "100000")
    run_db(ctx, scenario)


def test_two_traders_cannot_read_or_write_each_others_profiles(ctx):
    async def scenario():
        a = await account(ctx, user=ALICE, login="700001", synced=True)
        b = await account(ctx, user=BOB, login="700002", host="mtr.broker-two.example")
        assert (await save(ctx, a, MASTER, 0)).status_code == 200
        assert (await ctx.http.get(f"{S}/connections/{a}", headers=auth(BOB))).status_code == 404
        hostile = await save(ctx, a, {**MASTER, "size": {"amount": "1", "currency": "USD"}}, 1, "bob-0001", user=BOB)
        assert hostile.status_code == 404 and "50000" not in hostile.text
        assert [p["connection_id"] for p in (await ctx.http.get(f"{S}/profiles", headers=auth(BOB))).json()["profiles"]] == [b]
        assert (await save(ctx, b, {"status": "saved", "type": "evaluation"}, 0, "bob-0002", user=BOB)).status_code == 200
        rows = sorted(await q(ctx, "SELECT user_id, data->>'type' FROM account_setup_profiles"))
        assert rows == [(ALICE, "master"), (BOB, "evaluation")]
        missing = await ctx.http.get(f"{S}/connections/999999", headers=auth(ALICE))
        assert missing.status_code == 404 and missing.json() == (await ctx.http.get(f"{S}/connections/{b}", headers=auth(ALICE))).json()
        assert (await ctx.http.get(f"{S}/profiles")).status_code == 401
        assert (await ctx.http.get(f"{S}/profiles", headers={"Authorization": "Bearer tti_not-a-session"})).status_code == 401
    run_db(ctx, scenario)


def test_profile_survives_a_reconnect_of_the_same_account(ctx):
    async def scenario():
        cid = await account(ctx)
        assert (await save(ctx, cid, MASTER, 0)).status_code == 200
        await q(ctx, "UPDATE mtapi_connections SET status = 'disconnected' WHERE id = :i", i=cid)
        assert (await ctx.http.get(f"{S}/connections/{cid}", headers=auth(ALICE))).status_code == 404
        again = await account(ctx)                                      # same host + login, new connection id
        body = (await ctx.http.get(f"{S}/connections/{again}", headers=auth(ALICE))).json()
        assert body["version"] == 1 and body["profile"]["type"] == "master"
    run_db(ctx, scenario)


def test_validation_rejects_bad_answers_without_echoing_them(ctx):
    async def scenario():
        cid = await account(ctx)
        bad = [{"size": {"amount": "50000", "currency": "usd1"}},
               {"master": {"payoutTarget": {"value": "4321", "unit": "percent"}}},
               {"master": {"goal": {"id": "other", "text": "   "}}},
               {"evaluation": {"phaseCount": 2, "currentPhase": 3}},
               {"size": {"amount": "-5", "currency": "USD"}},
               {"surprise": "field-7777"},
               {"draft": {"x": "y" * 5000}}]
        for i, profile in enumerate(bad):
            res = await save(ctx, cid, profile, 0, f"bad-{i:05d}")
            assert res.status_code == 422, (profile, res.text)
            assert "4321" not in res.text and "field-7777" not in res.text and "usd1" not in res.text
        one = await save(ctx, cid, {"type": "evaluation", "evaluation": {"phaseCount": 1, "currentPhase": 1,
                                    "phaseTarget": {"value": "8", "unit": "percent", "base": "starting"}}}, 0, "ok-00001")
        assert one.status_code == 200 and "currentPhase" not in one.json()["profile"]["evaluation"]     # one phase: no "which phase"
        custom = await save(ctx, cid, {"type": "master", "size": {"amount": "15000", "currency": "EUR"}}, 1, "ok-00002")
        assert custom.json()["profile"]["size"] == {"amount": "15000", "currency": "EUR"}
        other = await save(ctx, cid, {"master": {"goal": {"id": "other", "text": "  Trade only London open "}}}, 2, "ok-00003")
        assert other.json()["profile"]["master"]["goal"] == {"id": "other", "text": "Trade only London open"}
    run_db(ctx, scenario)


def test_both_branches_are_kept_when_the_type_changes(ctx):
    async def scenario():
        cid = await account(ctx)
        both = {"type": "master", "evaluation": {"phaseCount": 2, "currentPhase": 1, "phaseTarget": {"value": "8", "unit": "percent", "base": "starting"}},
                "master": {"goal": {"id": "grow"}}}
        assert (await save(ctx, cid, both, 0)).status_code == 200
        back = (await save(ctx, cid, {**both, "type": "evaluation"}, 1, "save-0002")).json()["profile"]
        assert back["evaluation"]["phaseCount"] == 2 and back["master"]["goal"]["id"] == "grow"
    run_db(ctx, scenario)


def test_unknown_reference_is_kept_as_entered():
    p = schemas.Profile.model_validate({"firmRules": {"dailyLoss": {"value": "4", "unit": "percent", "base": "sod_equity"}, "dailyReset": "17:00"}})
    assert schemas.dump(p)["firmRules"]["dailyLoss"] == {"value": "4", "unit": "percent", "base": "sod_equity"}
    with pytest.raises(Exception):
        schemas.Profile.model_validate({"firmRules": {"dailyReset": "25:00"}})
