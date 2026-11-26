"""Feature flags and tunables. Everything defaults to OFF / conservative."""
from __future__ import annotations

import os

PROVIDER = "matchtrader_trader_api"


def _flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(str(os.getenv(name) or "").strip() or default))
    except ValueError:
        return default


def api_enabled() -> bool:
    """Connection routes + schema creation. Off => routes answer 404 and no tables are created."""
    return _flag("TALITRADE_MT_API_ENABLED")


def worker_enabled() -> bool:
    """Background polling. Requires api_enabled() as well."""
    return api_enabled() and _flag("TALITRADE_MT_API_WORKER_ENABLED")


def live_interval_seconds() -> int:
    return _int("TALITRADE_MT_API_LIVE_INTERVAL_SECONDS", 30, 10)


def history_interval_seconds() -> int:
    return _int("TALITRADE_MT_API_HISTORY_INTERVAL_SECONDS", 120, 30)


def history_overlap_seconds() -> int:
    return _int("TALITRADE_MT_API_HISTORY_OVERLAP_SECONDS", 900, 0)


def reconcile_window_seconds() -> int:
    """How far back the periodic wide re-check looks for late broker corrections."""
    return _int("TALITRADE_MT_API_RECONCILE_WINDOW_SECONDS", 48 * 3600, 0)


def reconcile_every_seconds() -> int:
    return _int("TALITRADE_MT_API_RECONCILE_EVERY_SECONDS", 6 * 3600, 0)


def backfill_intervals_per_cycle() -> int:
    return _int("TALITRADE_MT_API_BACKFILL_INTERVALS_PER_CYCLE", 2, 0)


def history_pass_seconds() -> int:
    """Wall-clock budget for history work in one pass. Remaining ranges stay queued for the next pass."""
    return _int("TALITRADE_MT_API_HISTORY_PASS_SECONDS", 12, 2)


def history_backlog_interval_seconds() -> int:
    """How soon history runs again while queued ranges remain (instead of the normal history interval)."""
    return _int("TALITRADE_MT_API_HISTORY_BACKLOG_INTERVAL_SECONDS", 20, 5)


def initial_backfill_days() -> int:
    """History queued automatically when an account is connected (0 = none)."""
    return _int("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", 7, 0)


def market_data_enabled() -> bool:
    """Market ticker and price chart (symbols, quotes, candles). Off => routes answer 404, no
    market tables are created and the worker never polls market endpoints."""
    return api_enabled() and _flag("TALITRADE_MT_API_MARKET_DATA_ENABLED")


def market_quote_interval_seconds() -> int:
    return _int("TALITRADE_MT_API_MARKET_QUOTE_INTERVAL_SECONDS", 15, 10)


def market_candle_interval_seconds() -> int:
    return _int("TALITRADE_MT_API_MARKET_CANDLE_INTERVAL_SECONDS", 60, 30)


def market_live_quote_interval_seconds() -> int:
    """Quote refresh for an instrument charted live (US30 only, for now). One batched /v1/quotes
    request per interval per account, shared by every open tab. 6 s = 10 requests/min, which fits
    the live share of the account-wide budget next to account sync (see ratelimit.LIVE_SHARE)."""
    return _int("TALITRADE_MT_API_MARKET_LIVE_QUOTE_INTERVAL_SECONDS", 6, 3)


def market_live_enabled() -> bool:
    """Live candle updates for the instruments in market.LIVE_INSTRUMENTS (US30 only, for now).
    Off by default: merging the code changes nothing until this is turned on for a rollout.
    Off => the live route answers 404 and the chart keeps using candle snapshots only."""
    return market_data_enabled() and _flag("TALITRADE_MT_API_MARKET_LIVE_ENABLED")


def market_symbols_interval_seconds() -> int:
    return _int("TALITRADE_MT_API_MARKET_SYMBOLS_INTERVAL_SECONDS", 300, 60)


def market_interest_seconds() -> int:
    """How long one dashboard read keeps market polling alive. Nobody reading => polling stops."""
    return _int("TALITRADE_MT_API_MARKET_INTEREST_SECONDS", 45, 20)


def market_candle_window() -> int:
    """Candles loaded for a chart (the provider allows at most 750 per request)."""
    return min(750, _int("TALITRADE_MT_API_MARKET_CANDLE_WINDOW", 300, 50))


def connect_enabled() -> bool:
    """Whether NEW connections may be created (and the dashboard may show the token form).

    Deliberately separate from api_enabled(): schema, routes and the worker can be deployed and
    verified first, with the form still closed."""
    return api_enabled() and _flag("TALITRADE_MT_API_CONNECT_ENABLED")


def service_status(user_id: str | None = None) -> dict:
    """Readiness gates as the dashboard sees them. No secrets, only booleans."""
    from app.services.matchtrader_api import crypto

    pilot_ids = os.getenv("TALITRADE_MT_API_PILOT_USER_IDS", "").split(",")
    pilot = bool(user_id) and api_enabled() and str(user_id) in {s.strip() for s in pilot_ids}
    status = {"secure_storage": crypto.is_configured(), "worker": worker_enabled(), "connect_enabled": connect_enabled() or pilot}
    status["accepting_connections"] = all(status.values())
    status["market_data"] = market_data_enabled() and worker_enabled()   # informational; not part of accepting_connections
    from app.services.account_setup import config as setup_config
    status["account_setup"] = setup_config.enabled()                     # the dashboard offers account setup only when this is on
    return status


LEASE_SECONDS = 90
SYNC_REQUEST_MAX_ATTEMPTS = 3
