"""OIDC replay protection on a real PostgreSQL: the SQL (ON CONFLICT ... RETURNING, TIMESTAMPTZ binds) and
concurrent submissions of the same id_token. The SQLite tests in test_oidc_nonce.py cannot show either.

Runs only when TALITRADE_TEST_DATABASE_URL names a disposable database; skipped otherwise."""
import asyncio
import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import oidc_replay  # noqa: E402

URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db_name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
pytestmark = pytest.mark.skipif(not URL or not ("test" in _db_name or "disposable" in _db_name),
                                reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")


def _claims(sub="tg-1"):
    return {"iss": "https://oauth.telegram.org", "sub": sub, "exp": int(time.time()) + 300}


async def _attempt(engine, claims, nonce):
    try:
        await oidc_replay.consume_nonce(claims, nonce, engine=engine)
        return "accepted"
    except ValueError as exc:
        return str(exc)


def _run(scenario, monkeypatch):
    monkeypatch.setattr(oidc_replay, "_table_ready", False)

    async def _main():
        engine = create_async_engine(URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS oidc_consumed_nonces"))
            await scenario(engine)
        finally:
            await engine.dispose()
    asyncio.run(_main())


def test_concurrent_submissions_of_one_token_accept_exactly_one(monkeypatch):
    # Fresh table and a cold process: the first logins also race on CREATE TABLE IF NOT EXISTS.
    async def scenario(engine):
        nonce = "n" * 32
        results = await asyncio.gather(*(_attempt(engine, _claims(), nonce) for _ in range(6)))
        assert results.count("accepted") == 1, results
        assert set(results) == {"accepted", "oidc_nonce_replayed"}, results
        assert await _attempt(engine, _claims(), nonce) == "oidc_nonce_replayed"   # and later, too
    _run(scenario, monkeypatch)


def test_distinct_nonces_and_subjects_are_independent(monkeypatch):
    async def scenario(engine):
        results = await asyncio.gather(
            *(_attempt(engine, _claims(sub=f"tg-{i % 2}"), f"nonce-{i:02d}-" + "x" * 20) for i in range(6)))
        assert results == ["accepted"] * 6, results
    _run(scenario, monkeypatch)


def test_expired_replay_records_are_purged_and_only_stored_as_hashes(monkeypatch):
    async def scenario(engine):
        nonce = "secret-nonce-" + "y" * 20
        assert await _attempt(engine, _claims(), nonce) == "accepted"
        async with engine.begin() as conn:
            stored = (await conn.execute(text("SELECT nonce_hash FROM oidc_consumed_nonces"))).scalars().all()
            assert stored == [oidc_replay.replay_key(_claims(), nonce)] and nonce not in stored[0]
            await conn.execute(text("UPDATE oidc_consumed_nonces SET expires_at = NOW() - INTERVAL '1 minute'"))
        assert await _attempt(engine, _claims(sub="other"), "z" * 32) == "accepted"   # triggers the purge
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT COUNT(*) FROM oidc_consumed_nonces"))).scalar() == 1
    _run(scenario, monkeypatch)
