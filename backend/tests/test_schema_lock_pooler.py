"""The startup schema lock through a real transaction-mode pooler (PgBouncer, as in Neon's pooled endpoint).

Production connects through Neon's pooler: PgBouncer in transaction mode, so a server connection goes back to
the pool after every COMMIT. These tests run the lock and the real startup schema steps through such a pooler:

  * a control proving the pooler really is in transaction mode (a session-level advisory lock leaks to the next
    client of the same server connection), so the remaining tests would catch a session-lock regression;
  * concurrent startups through the pooler never overlap their schema phases;
  * the lock is gone once the schema phase ends, even on the pooled server connection that held it.

Runs only when TALITRADE_TEST_POOLER_URL (pool of several server connections) and
TALITRADE_TEST_POOLER_SINGLE_URL (pool of exactly one server connection) point at a PgBouncer in front of the
disposable database in TALITRADE_TEST_DATABASE_URL. The CI job backend-integration-tests starts one."""
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
from app.core import database, schema_lock  # noqa: E402

DIRECT_URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
POOLER_URL = os.getenv("TALITRADE_TEST_POOLER_URL", "")
POOLER_SINGLE_URL = os.getenv("TALITRADE_TEST_POOLER_SINGLE_URL", "")


def _disposable(url):
    name = url.rsplit("/", 1)[-1].split("?")[0].lower()
    return bool(url) and ("test" in name or "disposable" in name)


pytestmark = pytest.mark.skipif(
    not (_disposable(DIRECT_URL) and _disposable(POOLER_URL) and _disposable(POOLER_SINGLE_URL)),
    reason="set TALITRADE_TEST_DATABASE_URL and TALITRADE_TEST_POOLER_URL / _SINGLE_URL (PgBouncer, transaction mode)")

LOCK_KEY = 7_412_026_998


def _engine(url):
    return create_async_engine(url, poolclass=NullPool)


async def _holders(direct, key):
    """Backend pids holding advisory lock `key`, read on a direct (non-pooled) connection."""
    async with direct.connect() as conn:
        rows = await conn.execute(text("""
            SELECT l.pid FROM pg_locks l
            WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
              AND ((l.classid::bigint << 32) | l.objid::bigint) = :k"""), {"k": key})
        return [r[0] for r in rows]


async def _terminate_holders(direct, key):
    async with direct.connect() as conn:
        for pid in await _holders(direct, key):
            await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        await conn.commit()


def _run(scenario):
    async def _main():
        direct = _engine(DIRECT_URL)
        try:
            await _terminate_holders(direct, LOCK_KEY)
            await scenario(direct)
        finally:
            await _terminate_holders(direct, LOCK_KEY)
            await direct.dispose()
    asyncio.run(_main())


def test_control_pooler_is_transaction_mode_and_leaks_session_locks():
    """Why the lock must be transaction-level: through this pooler a session lock outlives its client."""
    async def scenario(direct):
        single = _engine(POOLER_SINGLE_URL)
        try:
            async with single.connect() as a:
                assert (await a.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})).scalar() is True
                await a.commit()                         # server connection goes back to the pool
            # Client A is gone, yet the lock is still held, by the pooled server connection.
            assert len(await _holders(direct, LOCK_KEY)) == 1
            async with single.connect() as b:            # an unrelated client on the same server connection
                again = (await b.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})).scalar()
                await b.commit()
            assert again is True, "pooler is not in transaction mode: the control cannot prove anything"
        finally:
            await single.dispose()
    _run(scenario)


def test_xact_lock_is_gone_from_the_pooled_server_connection_after_the_schema_phase():
    async def scenario(direct):
        single_a, single_b = _engine(POOLER_SINGLE_URL), _engine(POOLER_SINGLE_URL)
        try:
            async with schema_lock.startup_schema_lock(single_a, key=LOCK_KEY, wait_seconds=5):
                assert len(await _holders(direct, LOCK_KEY)) == 1
            assert await _holders(direct, LOCK_KEY) == []
            # The next client of that same server connection must be able to take it (not inherit it).
            async with schema_lock.startup_schema_lock(single_b, key=LOCK_KEY, wait_seconds=5):
                assert len(await _holders(direct, LOCK_KEY)) == 1
            assert await _holders(direct, LOCK_KEY) == []
        finally:
            await single_a.dispose()
            await single_b.dispose()
    _run(scenario)


def test_second_instance_waits_through_the_pooler_while_the_first_holds_the_lock():
    async def scenario(direct):
        first, second = _engine(POOLER_URL), _engine(POOLER_URL)
        try:
            async with schema_lock.startup_schema_lock(first, key=LOCK_KEY, wait_seconds=5):
                # Committed work on other pooled connections must not release the holder's lock.
                for _ in range(5):
                    async with first.begin() as conn:
                        await conn.execute(text("SELECT 1"))
                with pytest.raises(schema_lock.SchemaLockTimeout):
                    async with schema_lock.startup_schema_lock(second, key=LOCK_KEY, wait_seconds=1.0):
                        pass
            async with schema_lock.startup_schema_lock(second, key=LOCK_KEY, wait_seconds=5):
                pass
        finally:
            await first.dispose()
            await second.dispose()
    _run(scenario)


def test_concurrent_startups_through_the_pooler_run_schema_steps_one_instance_at_a_time(monkeypatch):
    """Three instances start together; each runs the real schema steps through the pooler. Their schema
    phases must not interleave, and every step must succeed."""
    pooled = _engine(POOLER_URL)
    original = database.engine
    monkeypatch.setattr(database, "engine", pooled)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", pooled)
    monkeypatch.setattr(main.mt_api_config, "api_enabled", lambda: False)
    monkeypatch.setattr(main.setup_config, "enabled", lambda: False)

    async def scenario(direct):
        # Start from an empty schema, like a first deploy: other tests leave reduced versions of app tables.
        from tests.test_worker_lifecycle_db import APP_TABLES
        async with direct.begin() as conn:
            for table in APP_TABLES:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        events = []

        async def instance(name):
            async def step(step_name, fn):
                events.append((name, step_name))
                try:
                    return await fn()
                except Exception as exc:  # test database only: show the real error, and who was in the lock
                    raise AssertionError(f"{name}:{step_name} failed: {exc!r} events={events}") from exc
            async with schema_lock.startup_schema_lock(pooled):
                events.append((name, "lock:in"))
                await main._run_schema_steps(step)
                events.append((name, "lock:out"))

        try:
            await asyncio.gather(instance("a"), instance("b"), instance("c"))
        finally:
            await pooled.dispose()
        owners = [owner for owner, _ in events]
        # Each instance's events are contiguous: lock:in, all its steps, lock:out, then the next instance.
        blocks = [owners[0]] + [o for prev, o in zip(owners, owners[1:]) if o != prev]
        assert sorted(blocks) == ["a", "b", "c"], events
        for name in "abc":
            mine = [e for o, e in events if o == name]
            assert mine[0] == "lock:in" and mine[-1] == "lock:out", mine
        assert await _holders(direct, schema_lock.STARTUP_SCHEMA_LOCK_KEY) == []
    _run(scenario)
