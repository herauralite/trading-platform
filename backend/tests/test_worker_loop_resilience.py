"""The connector sync worker loop must survive a failing poll instead of dying for the process lifetime."""
import asyncio
import logging
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services import connector_ingest as ci  # noqa: E402


def test_worker_loop_keeps_polling_after_errors(monkeypatch, caplog):
    calls = []
    stop = asyncio.Event()

    async def flaky(*, worker_id):
        calls.append(worker_id)
        if len(calls) <= 2:
            raise RuntimeError("postgres://u:secret@host AmbiguousParameterError")
        stop.set()
        return None

    monkeypatch.setattr(ci, "run_connector_sync_once", flaky)
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout):
        return await real_wait_for(aw, timeout=min(timeout, 0.01))
    monkeypatch.setattr(ci.asyncio, "wait_for", fast_wait_for)

    with caplog.at_level(logging.ERROR):
        asyncio.run(ci.connector_sync_worker_loop(stop, worker_id="w1"))
    assert len(calls) == 3
    assert "exception_type=RuntimeError consecutive=2" in caplog.text and "secret" not in caplog.text


def test_worker_loop_stops_promptly_on_cancel(monkeypatch):
    async def idle(*, worker_id):
        return None
    monkeypatch.setattr(ci, "run_connector_sync_once", idle)

    async def scenario():
        task = asyncio.create_task(ci.connector_sync_worker_loop(asyncio.Event(), worker_id="w1", idle_poll_seconds=10))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False
    assert asyncio.run(scenario()) is True
