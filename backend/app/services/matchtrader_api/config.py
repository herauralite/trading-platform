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


LEASE_SECONDS = 90
