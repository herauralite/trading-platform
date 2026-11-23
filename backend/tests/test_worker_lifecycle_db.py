"""Worker lifecycle and schema safety against a real PostgreSQL: concurrent startups, restarts, crashed
lock holders, duplicate job claims, expired leases, and once-only Telegram delivery across instances.

Runs only when TALITRADE_TEST_DATABASE_URL names a disposable database ("test"/"disposable" in its name);
app tables are dropped and recreated. Skipped otherwise."""
import asyncio
import os
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import database, health, schema_lock  # noqa: E402
from app.services import community_bot, connector_ingest as ci  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")

APP_TABLES = ("account_snapshots", "connector_configs", "connector_events", "connector_lifecycle", "connector_sync_runs",
              "demo_scores", "integration_tokens", "mt5_bridge_accounts", "mt5_pairing_tokens", "mt5_trusted_bridges",
              "positions", "prop_accounts", "public_api_beta_connections", "telegram_alert_preferences",
              "telegram_alert_receipts", "trades", "trading_accounts", "tradingview_webhook_connections", "users",
              "verified_waitlist", "waitlist")
TEST_LOCK_KEY = 7_412_026_999


@pytest.fixture
def db(monkeypatch):
    original = database.engine
    engine = create_async_engine(URL, poolclass=NullPool)
    monkeypatch.setattr(database, "engine", engine)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(main.mt_api_config, "api_enabled", lambda: False)
    monkeypatch.setattr(main.setup_config, "enabled", lambda: False)
    return engine


def run(engine, scenario, *, fresh=True):
    async def _main():
        if fresh:
            async with engine.begin() as conn:
                for table in APP_TABLES:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        try:
            await scenario()
        finally:
            await engine.dispose()
    asyncio.run(_main())


async def bootstrap(engine):
    async with schema_lock.startup_schema_lock(engine):
        await main._run_schema_steps(health.run_startup_step)


async def tables(engine):
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        return {r[0] for r in rows}


# ── schema bootstrap ─────────────────────────────────────────────────────────

def test_concurrent_startups_bootstrap_the_schema_once_without_errors(db):
    async def scenario():
        await asyncio.gather(bootstrap(db), bootstrap(db), bootstrap(db))   # three instances starting together
        assert {"trades", "users", "trading_accounts", "positions", "connector_sync_runs",
                "integration_tokens", "telegram_alert_receipts"} <= await tables(db)
        await bootstrap(db)                                                  # a restart: idempotent
    run(db, scenario)


def test_restart_preserves_existing_rows(db):
    async def scenario():
        await bootstrap(db)
        await ci.upsert_trading_account({"user_id": "u1", "connector_type": "fundingpips_extension",
                                         "external_account_id": "FP-1"})
        async with db.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_user_id) VALUES ('u1')"))
            await conn.execute(text("INSERT INTO trades (account_id, symbol, direction, pnl, closed_at) VALUES ('FP-1','US30','buy',5,NOW())"))
        await bootstrap(db)
        await bootstrap(db)
        async with db.connect() as conn:
            counts = [(await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar() for t in ("users", "trading_accounts", "trades")]
        assert counts == [1, 1, 1]
    run(db, scenario)


def test_legacy_users_table_is_never_dropped_at_startup(db):
    async def scenario():
        async with db.begin() as conn:
            await conn.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY, telegram_id BIGINT, username TEXT)"))
            await conn.execute(text("INSERT INTO users (telegram_id, username) VALUES (42, 'keep-me')"))
        with pytest.raises(RuntimeError, match="refusing to drop"):
            await main.ensure_users_tables()
        async with db.connect() as conn:
            assert (await conn.execute(text("SELECT username FROM users"))).scalar() == "keep-me"
    run(db, scenario)


# ── the advisory lock itself ─────────────────────────────────────────────────

def test_schema_lock_excludes_a_second_instance_until_released(db):
    async def scenario():
        order = []

        async def first():
            async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY):
                order.append("first:in")
                await asyncio.sleep(1.0)
                order.append("first:out")

        async def second():
            await asyncio.sleep(0.2)
            async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY, wait_seconds=10):
                order.append("second:in")

        await asyncio.gather(first(), second())
        assert order == ["first:in", "first:out", "second:in"]
    run(db, scenario, fresh=False)


def test_schema_lock_times_out_instead_of_hanging(db):
    async def scenario():
        async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY):
            with pytest.raises(schema_lock.SchemaLockTimeout):
                async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY, wait_seconds=0.6):
                    pass
    run(db, scenario, fresh=False)


def test_lock_held_by_a_crashed_instance_is_released_with_its_connection(db):
    async def scenario():
        crashed = await db.connect()
        assert (await crashed.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": TEST_LOCK_KEY})).scalar() is True
        await crashed.commit()
        await crashed.invalidate()   # the process died: its connection is gone without an unlock
        async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY, wait_seconds=10):
            pass
    run(db, scenario, fresh=False)


async def _lock_holders(engine, key):
    """(pid, backend state) of every session holding advisory lock `key` (bigint keys: classid=high, objid=low)."""
    async with engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT l.pid, a.state FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
              AND ((l.classid::bigint << 32) | l.objid::bigint) = :k"""), {"k": key})
        return [(r[0], r[1]) for r in rows]


def test_schema_lock_is_held_by_an_open_transaction_so_a_transaction_pooler_cannot_leak_it(db):
    """Production uses Neon's PgBouncer pooler (transaction mode): a server connection is handed to another
    client after every COMMIT. The lock must therefore be held inside an open transaction for its whole life
    (a backend that is 'idle in transaction' stays pinned), never by an idle session, and gone afterwards."""
    async def scenario():
        async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY):
            await asyncio.sleep(0.2)
            holders = await _lock_holders(db, TEST_LOCK_KEY)
            assert len(holders) == 1, holders
            assert holders[0][1] == "idle in transaction", holders
        assert await _lock_holders(db, TEST_LOCK_KEY) == []
    run(db, scenario, fresh=False)


def test_schema_lock_is_released_when_a_startup_step_fails(db):
    async def scenario():
        with pytest.raises(RuntimeError):
            async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY):
                raise RuntimeError("step failed")
        assert await _lock_holders(db, TEST_LOCK_KEY) == []
        async with schema_lock.startup_schema_lock(db, key=TEST_LOCK_KEY, wait_seconds=1):
            pass
    run(db, scenario, fresh=False)


# ── background sync jobs ─────────────────────────────────────────────────────

def test_a_queued_sync_run_is_claimed_by_exactly_one_of_many_workers(db):
    async def scenario():
        await bootstrap(db)
        run_row = await ci.create_connector_sync_run("u1", "fundingpips_extension")
        claims = await asyncio.gather(*(ci.claim_next_connector_sync_run(worker_id=f"w{i}") for i in range(8)))
        winners = [c for c in claims if c]
        assert len(winners) == 1 and winners[0]["id"] == run_row["id"]
        assert await ci.claim_next_connector_sync_run(worker_id="late") is None
    run(db, scenario)


def test_expired_lease_is_recovered_after_a_crash_and_the_old_owner_is_fenced(db):
    async def scenario():
        await bootstrap(db)
        run_row = await ci.create_connector_sync_run("u1", "fundingpips_extension")
        assert (await ci.claim_next_connector_sync_run(worker_id="crashed"))["id"] == run_row["id"]
        assert await ci.claim_next_connector_sync_run(worker_id="other") is None   # lease still valid
        async with db.begin() as conn:                                               # the lease runs out
            await conn.execute(text("UPDATE connector_sync_runs SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = :i"), {"i": run_row["id"]})
        recovered = await ci.claim_next_connector_sync_run(worker_id="recovery")
        assert recovered["id"] == run_row["id"] and recovered["lease_owner"] == "recovery"
        # The original worker comes back late: its result is refused, the recovering worker's stands.
        with pytest.raises(ValueError):
            await ci._set_sync_run_status(run_row["id"], status="succeeded", expected_status="running",
                                          lease_owner="crashed", clear_lease=True)
        done = await ci._set_sync_run_status(run_row["id"], status="succeeded", expected_status="running",
                                             lease_owner="recovery", clear_lease=True)
        assert done["status"] == "succeeded"
    run(db, scenario)


# ── Telegram once-only across instances / restarts ───────────────────────────

def test_concurrent_delivery_of_the_same_alert_sends_once(db, monkeypatch):
    async def scenario():
        await bootstrap(db)
        await community_bot.subscribe("u1", True)
        sent = []

        async def fake_api(client, method, *, data=None, files=None):
            await asyncio.sleep(0.05)
            sent.append((method, data["chat_id"]))
        monkeypatch.setattr(community_bot, "api", fake_api)
        await asyncio.gather(*(community_bot.deliver("u1", "weekend:2026-11-27_16_0", "Friday close") for _ in range(6)))
        assert sent == [("sendMessage", "u1")]
        await community_bot.deliver("u2-not-subscribed", "weekend:2026-11-27_16_0", "x")
        assert len(sent) == 1
    run(db, scenario)


def test_failed_send_is_retried_later_not_lost(db, monkeypatch):
    async def scenario():
        await bootstrap(db)
        await community_bot.subscribe("u1", True)
        attempts = []

        async def flaky_api(client, method, *, data=None, files=None):
            attempts.append(method)
            if len(attempts) == 1:
                raise RuntimeError("telegram 502")
        monkeypatch.setattr(community_bot, "api", flaky_api)
        with pytest.raises(RuntimeError):
            await community_bot.deliver("u1", "summary:2026-11-23", "report")
        await community_bot.deliver("u1", "summary:2026-11-23", "report")      # next pass (or next instance)
        await community_bot.deliver("u1", "summary:2026-11-23", "report")
        assert attempts == ["sendMessage", "sendMessage"]
    run(db, scenario)


def test_legacy_daily_summary_claim_survives_restart_and_other_instances(db):
    async def scenario():
        await bootstrap(db)
        results = await asyncio.gather(*(community_bot.claim_once("u1", "summary-legacy:2026-11-23") for _ in range(5)))
        assert sorted(results) == [False] * 4 + [True]
        await bootstrap(db)                                                     # restart within the send window
        assert await community_bot.claim_once("u1", "summary-legacy:2026-11-23") is False
        assert await community_bot.claim_once("u1", "summary-legacy:2026-11-24") is True
    run(db, scenario)


# ── statements that only fail on a real PostgreSQL ───────────────────────────

def test_tradingview_webhook_event_is_stored_end_to_end(db):
    """Previously failed on every call: missing FROM alias and untyped jsonb_build_object parameters."""
    async def scenario():
        from app.services import provider_onboarding as po
        await bootstrap(db)
        conn = await po.create_tradingview_connection(user_id="u1", display_label="TV alerts")
        out = await po.ingest_tradingview_event(token=conn["webhook_token"],
                                                payload={"symbol": "US30", "timeframe": "5", "message": "breakout"})
        assert out["ok"] is True and out["state"] == "active"
        async with db.connect() as c:
            events = (await c.execute(text("SELECT COUNT(*) FROM connector_events WHERE connector_type = 'tradingview_webhook'"))).scalar()
        assert events == 1
    run(db, scenario)


def test_full_sync_pipeline_runs_a_queued_job_to_completion_once(db, monkeypatch):
    """Queue -> worker claim -> execute -> final status, with only the provider call stubbed."""
    async def scenario():
        await bootstrap(db)
        calls = []

        async def fake_perform(run):
            calls.append(run["id"])
            return {"accounts": 1, "trades": 0}
        monkeypatch.setattr(ci, "_perform_connector_sync", fake_perform)
        queued = await ci.create_connector_sync_run("u1", "fundingpips_extension")
        results = await asyncio.gather(*(ci.run_connector_sync_once(worker_id=f"w{i}") for i in range(4)))
        assert calls == [queued["id"]] and sum(1 for r in results if r) == 1
        async with db.connect() as c:
            row = (await c.execute(text("SELECT status, lease_owner, finished_at FROM connector_sync_runs WHERE id = :i"),
                                   {"i": queued["id"]})).mappings().first()
        assert row["status"] == "succeeded" and row["lease_owner"] is None and row["finished_at"] is not None
        assert await ci.run_connector_sync_once(worker_id="again") is None
    run(db, scenario)
