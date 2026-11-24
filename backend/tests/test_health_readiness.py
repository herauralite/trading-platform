"""Liveness vs readiness, sanitized failures, and startup-step / background-task supervision."""
import asyncio
import logging
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import health  # noqa: E402


class _Conn:
    def __init__(self, fail):
        self.fail = fail

    async def __aenter__(self):
        if self.fail == "connect":
            raise OSError("could not connect to db-host.internal:5432 password=hunter2")
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        if self.fail == "hang":
            await asyncio.sleep(60)
        return None


class _Engine:
    def __init__(self, fail=None):
        self.fail = fail

    def connect(self):
        return _Conn(self.fail)


def _get(path, engine):
    async def _run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)
    original = main.engine
    main.engine = engine
    try:
        return asyncio.run(_run())
    finally:
        main.engine = original


@pytest.fixture(autouse=True)
def _reset_state():
    health.startup_state.reset()
    yield
    health.startup_state.reset()


def test_liveness_does_not_touch_the_database():
    response = _get("/health", _Engine(fail="connect"))
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_readiness_is_503_until_startup_completes():
    response = _get("/health/ready", _Engine())
    assert response.status_code == 503
    assert response.json()["status"] == "starting"


def test_readiness_ok_after_startup_with_reachable_database():
    health.startup_state.complete = True
    response = _get("/health/ready", _Engine())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"startup": "ok", "database": "ok"}}


@pytest.mark.parametrize("path", ["/health/ready", "/health/db"])
def test_database_failure_is_503_and_sanitized(path, caplog):
    health.startup_state.complete = True
    with caplog.at_level(logging.WARNING):
        response = _get(path, _Engine(fail="connect"))
    assert response.status_code == 503
    body = response.text
    for leaked in ("db-host", "5432", "hunter2", "OSError", "Traceback"):
        assert leaked not in body
    assert "hunter2" not in caplog.text and "db-host" not in caplog.text
    assert "exception_type=OSError" in caplog.text


def test_readiness_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(health, "READINESS_DB_TIMEOUT_SECONDS", 0.05)
    assert asyncio.run(health.check_database(_Engine(fail="hang"), timeout=0.05)) is False


def test_failed_startup_step_is_reraised_logged_by_type_and_blocks_readiness(caplog):
    async def boom():
        raise RuntimeError("postgres://user:secret@host/db refused")

    with caplog.at_level(logging.ERROR), pytest.raises(health.StartupStepFailed) as raised:
        asyncio.run(health.run_startup_step("ensure_trades_table", boom))
    assert "secret" not in str(raised.value) and raised.value.__suppress_context__
    assert "step=ensure_trades_table" in caplog.text and "exception_type=RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert health.startup_state.failed_step == "ensure_trades_table"
    response = _get("/health/ready", _Engine())
    assert response.status_code == 503 and response.json()["checks"]["startup"] == "failed"


def test_background_task_crash_is_logged_not_lost(caplog):
    async def worker():
        raise ValueError("token=abc123")

    async def _run():
        task = health.supervise_task(asyncio.create_task(worker()), "news_scheduler")
        await asyncio.wait([task])
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run())
    assert "task=news_scheduler" in caplog.text and "exception_type=ValueError" in caplog.text
    assert "abc123" not in caplog.text


def test_lifespan_runs_every_step_marks_ready_and_shuts_tasks_down(monkeypatch):
    """Full lifespan with every DB/network dependency stubbed: ready after startup, not ready after shutdown."""
    calls = []

    def stub(name):
        async def _f(*a, **k):
            calls.append(name)
        return _f

    for name in ("ensure_trades_table", "ensure_users_tables", "ensure_connector_tables",
                 "backfill_legacy_prop_accounts_to_trading_accounts", "ensure_waitlist_table",
                 "ensure_demo_scores_table", "setup_telegram_webhook"):
        monkeypatch.setattr(main, name, stub(name))
    monkeypatch.setattr(main.ingest_auth, "ensure_integration_tokens_table", stub("tokens"))
    monkeypatch.setattr(main.mt_api_config, "api_enabled", lambda: False)
    monkeypatch.setattr(main.mt_api_config, "worker_enabled", lambda: False)
    monkeypatch.setattr(main.setup_config, "enabled", lambda: False)

    async def forever(*a, **k):
        await asyncio.Event().wait()

    for name in ("news_scheduler", "weekend_scheduler", "daily_summary_scheduler", "connector_sync_worker_loop"):
        monkeypatch.setattr(main, name, forever)
    from app.services import community_bot
    monkeypatch.setattr(community_bot, "setup", stub("community_bot_setup"))
    monkeypatch.setattr(community_bot, "run", forever)

    async def _run():
        async with main.lifespan(main.app):
            assert health.startup_state.complete is True
        assert health.startup_state.complete is False

    asyncio.run(_run())
    assert calls[:3] == ["ensure_trades_table", "ensure_users_tables", "ensure_connector_tables"]
    assert "community_bot_setup" in calls and "setup_telegram_webhook" in calls


_FAILING_APP = '''
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core import health

async def boom():
    # Built at runtime, like a driver error built from DATABASE_URL: the traceback's source lines can't show it.
    raise RuntimeError(os.environ["LEAK_CANARY_DSN"] + " refused")

@asynccontextmanager
async def lifespan(app):
    await health.run_startup_step("ensure_trades_table", boom)
    yield

app = FastAPI(lifespan=lifespan)
'''


def test_real_uvicorn_startup_failure_exits_nonzero_without_leaking_exception_text(tmp_path):
    """End-to-end through uvicorn + Starlette: Starlette hands uvicorn the formatted traceback of a
    failed lifespan, and uvicorn logs it verbatim. The re-raised error must not carry the original text."""
    import subprocess
    import sys

    (tmp_path / "failing_app.py").write_text(_FAILING_APP)
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(tmp_path), backend_dir]),
        LEAK_CANARY_DSN="postgresql://user:CANARY-hunter2@db-host.internal/db",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "failing_app:app", "--port", "0", "--lifespan", "on"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 3, output  # uvicorn's STARTUP_FAILURE exit code
    assert "Application startup failed" in output
    assert "step=ensure_trades_table" in output and "exception_type=RuntimeError" in output
    assert "CANARY" not in output and "db-host.internal" not in output, output
