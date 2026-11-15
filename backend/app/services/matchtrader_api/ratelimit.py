"""Per-account request budgeting for the Trader API.

Documented limits are per endpoint, per account, per sliding minute, plus an account-visible
global ceiling that is not published. This limiter therefore:
* keeps a sliding-window count per endpoint with safety headroom below the documented ceiling;
* obeys ``X-RateLimit-Remaining-*`` headers when present (other clients can spend the same
  budget), and ``Retry-After`` on 429;
* lets low-priority work (backfill) yield to live sync by reserving part of each budget.

State is in-process. TaliTrade currently runs as a single process (WEB_CONCURRENCY=1 is
enforced at import), and each connection has one leased writer, so this is sufficient today.
Running several replicas requires moving these counters to shared storage first.
"""
from __future__ import annotations

import os
import random
import time
from collections import defaultdict, deque

ENDPOINT_LIMITS_PER_MINUTE = {
    "/v1/platform-details": 10,
    "/v1/account": 10,
    "/v1/account-summary": 20,
    "/v1/positions": 20,
    "/v1/pending-orders": 20,
    "/v1/closed-positions": 20,
    "/v1/ledgers": 20,
}
GLOBAL_PER_MINUTE_ENV = "TALITRADE_MT_API_GLOBAL_PER_MINUTE"
DEFAULT_GLOBAL_PER_MINUTE = 30   # the real account-wide ceiling is not published: stay well under the endpoint sum (120)
HEADROOM = 0.7          # use at most 70% of a documented endpoint budget
BACKFILL_SHARE = 0.5    # low-priority callers may use at most half of the usable budget
WINDOW_SECONDS = 60.0


class BudgetExhausted(Exception):
    def __init__(self, path: str, retry_in: float):
        super().__init__(f"rate budget exhausted for {path}")
        self.path, self.retry_in = path, retry_in


class AccountRateBudget:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._calls: dict[str, deque] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def _trim(self, path: str, now: float) -> deque:
        calls = self._calls[path]
        while calls and now - calls[0] >= WINDOW_SECONDS:
            calls.popleft()
        return calls

    def reserve(self, path: str, *, low_priority: bool = False) -> None:
        """Count one request against the endpoint AND account-wide budgets, or raise BudgetExhausted.
        Failed requests count too."""
        now = self._clock()
        for key in (path, "*"):
            until = self._blocked_until.get(key, 0.0)
            if until > now:
                raise BudgetExhausted(path, until - now)
        calls = self._trim(path, now)
        everything = self._trim("*", now)
        usable = max(1, int(ENDPOINT_LIMITS_PER_MINUTE.get(path, 10) * HEADROOM))
        allowed = max(1, int(usable * BACKFILL_SHARE)) if low_priority else usable
        if len(calls) >= allowed:
            raise BudgetExhausted(path, WINDOW_SECONDS - (now - calls[0]))
        global_allowed = max(1, int(global_limit_per_minute() * BACKFILL_SHARE)) if low_priority else global_limit_per_minute()
        if len(everything) >= global_allowed:
            raise BudgetExhausted(path, WINDOW_SECONDS - (now - everything[0]))
        calls.append(now)
        everything.append(now)

    def observe_headers(self, path: str, remaining_endpoint: int | None, remaining_global: int | None) -> None:
        now = self._clock()
        if remaining_endpoint is not None and remaining_endpoint <= 0:
            self._blocked_until[path] = max(self._blocked_until.get(path, 0.0), now + WINDOW_SECONDS / 2)
        if remaining_global is not None and remaining_global <= 1:
            self._blocked_until["*"] = max(self._blocked_until.get("*", 0.0), now + WINDOW_SECONDS / 2)

    def penalize(self, path: str, retry_after_seconds: float | None, *, global_scope: bool = False) -> float:
        """Record a 429. Returns the pause applied (Retry-After plus jitter)."""
        pause = float(retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 30.0)
        pause += random.uniform(0.5, 3.0)
        key = "*" if global_scope else path
        self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), self._clock() + pause)
        return pause


def global_limit_per_minute() -> int:
    """Account-wide request budget across all endpoints (configurable; conservative by default)."""
    try:
        return max(5, int(os.getenv(GLOBAL_PER_MINUTE_ENV) or DEFAULT_GLOBAL_PER_MINUTE))
    except ValueError:
        return DEFAULT_GLOBAL_PER_MINUTE


_budgets: dict[str, AccountRateBudget] = {}


def budget_for(account_key: str) -> AccountRateBudget:
    """One budget per verified external account, shared by worker, manual sync and backfill."""
    return _budgets.setdefault(account_key, AccountRateBudget())
