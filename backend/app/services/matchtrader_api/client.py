"""GET-only HTTP client for the Match-Trader Trader API.

Hard guarantees enforced here, independent of what the token is permitted to do:
* the host is fixed; no URL from user input or from an API response is ever fetched;
* only GET, and only the read paths in ``ALLOWED_PATHS``;
* redirects are never followed (a redirect would forward the Authorization header);
* TLS verification stays on; timeouts and a response-size cap apply;
* the token never appears in exceptions, reprs or log output from this module;
* an error is always an exception, never an empty "successful" result.
"""
from __future__ import annotations

import json
import time
import logging
from typing import Any

import httpx

from app.services.matchtrader_api import schemas
from app.services.matchtrader_api.ratelimit import AccountRateBudget

logger = logging.getLogger("app.matchtrader_api")

BASE_URL = "https://trader-api.match-trader.com"
ALLOWED_PATHS = frozenset({
    "/v1/platform-details", "/v1/account", "/v1/account-summary", "/v1/positions",
    "/v1/pending-orders", "/v1/closed-positions", "/v1/ledgers",
})
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


class MatchTraderApiError(Exception):
    """Base error. Carries only the path, HTTP status and the problem ``type`` URI/slug."""

    def __init__(self, path: str, status: int | None = None, problem_type: str | None = None, retry_after: float | None = None):
        super().__init__(f"{type(self).__name__} path={path} status={status} type={problem_type}")
        self.path, self.status, self.problem_type, self.retry_after = path, status, problem_type, retry_after


class Unauthorized(MatchTraderApiError):
    """401: token invalid, expired or revoked (indistinguishable). Needs a replacement token."""


class Forbidden(MatchTraderApiError):
    """403: this capability is not permitted for the token or by the broker."""


class RateLimited(MatchTraderApiError):
    """429."""


class TransientError(MatchTraderApiError):
    """Network failure, timeout or 5xx. Safe to retry later with backoff."""


class RequestRejected(MatchTraderApiError):
    """Other 4xx. Not retried."""


class ContractError(MatchTraderApiError):
    """Redirect, oversized body, invalid JSON or schema mismatch."""


def _int_header(response: httpx.Response, name: str) -> int | None:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError):
        return None


class _Secret:
    """Holds the bearer token so it cannot leak through repr(), vars() or tracebacks with locals."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__


class MatchTraderApiClient:
    def __init__(self, token: str, *, budget: AccountRateBudget | None = None, transport: httpx.AsyncBaseTransport | None = None):
        if not token or any(ch.isspace() for ch in token):
            raise ValueError("a bearer token is required")
        self.__token = _Secret(token)
        self._budget = budget
        self.timings: list[tuple[str, float]] = []   # (path, seconds) per request: durations only, for pass metrics
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, timeout=TIMEOUT, follow_redirects=False, verify=True, transport=transport,
            headers={"Accept": "application/json", "User-Agent": "TaliTrade-ReadOnly/1.0"},
        )

    def __repr__(self) -> str:
        return "MatchTraderApiClient(token=***)"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: list[tuple[str, Any]] | None = None, *, low_priority: bool = False) -> Any:
        if path not in ALLOWED_PATHS:
            raise ValueError(f"path not allowed: {path}")
        if self._budget is not None:
            self._budget.reserve(path, low_priority=low_priority)  # counts failures too
        request = self._http.build_request("GET", path, params=params, headers={"Authorization": f"Bearer {self.__token.reveal()}"})
        started = time.monotonic()
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            self.timings.append((path, time.monotonic() - started))
            raise TransientError(path, None, type(exc).__name__) from None
        try:
            if self._budget is not None:
                self._budget.observe_headers(path, _int_header(response, "X-RateLimit-Remaining-Endpoint"), _int_header(response, "X-RateLimit-Remaining-Global"))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ContractError(path, response.status_code, "response_too_large")
        except httpx.HTTPError as exc:
            raise TransientError(path, None, type(exc).__name__) from None
        finally:
            await response.aclose()
            self.timings.append((path, time.monotonic() - started))

        status = response.status_code
        if status == 200:
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ContractError(path, status, "invalid_json") from None
        problem_type = None
        try:  # RFC 9457 problem details: branch on status + stable ``type``, never on ``detail`` prose
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                problem_type = parsed["type"][:200]
        except (ValueError, UnicodeDecodeError):
            pass
        if 300 <= status < 400:
            raise ContractError(path, status, "unexpected_redirect")
        if status == 401:
            raise Unauthorized(path, status, problem_type)
        if status == 403:
            raise Forbidden(path, status, problem_type)
        if status == 429:
            retry_after = None
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except ValueError:
                pass
            if self._budget is not None:
                # Scope of the 429: the endpoint only when the headers prove the account budget still
                # has room and this endpoint is the one exhausted. Anything ambiguous pauses the whole
                # account, because guessing "endpoint" would keep hammering an exhausted account.
                remaining_global = _int_header(response, "X-RateLimit-Remaining-Global")
                remaining_endpoint = _int_header(response, "X-RateLimit-Remaining-Endpoint")
                endpoint_only = remaining_endpoint == 0 and remaining_global is not None and remaining_global > 0
                self._budget.penalize(path, retry_after, global_scope=not endpoint_only)
            raise RateLimited(path, status, problem_type, retry_after)
        if status >= 500:
            raise TransientError(path, status, problem_type)
        raise RequestRejected(path, status, problem_type)

    def _parse(self, path: str, fn, *args):
        try:
            return fn(*args)
        except schemas.ContractViolation as exc:
            logger.warning("matchtrader contract violation path=%s detail=%s", path, exc)
            raise ContractError(path, 200, "schema_mismatch") from None

    # ── typed reads ──────────────────────────────────────────────────────────
    async def platform_details(self) -> schemas.PlatformDetails:
        return self._parse("/v1/platform-details", schemas.parse_object, await self._get("/v1/platform-details"), schemas.PlatformDetails)

    async def account(self) -> schemas.AccountProfile:
        return self._parse("/v1/account", schemas.parse_object, await self._get("/v1/account"), schemas.AccountProfile)

    async def account_summary(self) -> schemas.AccountSummary:
        return self._parse("/v1/account-summary", schemas.parse_object, await self._get("/v1/account-summary"), schemas.AccountSummary)

    async def positions(self) -> list[schemas.OpenPosition]:
        """Always unfiltered: only a full, validated response may reconcile the stored set."""
        return self._parse("/v1/positions", schemas.parse_envelope, await self._get("/v1/positions"), "positions", schemas.OpenPosition)

    async def pending_orders(self) -> list[schemas.PendingOrder]:
        return self._parse("/v1/pending-orders", schemas.parse_envelope, await self._get("/v1/pending-orders"), "pendingOrders", schemas.PendingOrder)

    async def closed_positions(self, range_from: int, range_to: int, *, limit: int = 500, low_priority: bool = False) -> list[schemas.ClosedPosition]:
        params = [("from", int(range_from)), ("to", int(range_to)), ("limit", int(limit))]
        payload = await self._get("/v1/closed-positions", params, low_priority=low_priority)
        return self._parse("/v1/closed-positions", schemas.parse_envelope, payload, "closedPositions", schemas.ClosedPosition)

    async def ledgers(self, range_from: int, range_to: int, *, limit: int = 500, low_priority: bool = False) -> list[schemas.LedgerEntry]:
        params = [("from", int(range_from)), ("to", int(range_to)), ("limit", int(limit))]
        payload = await self._get("/v1/ledgers", params, low_priority=low_priority)
        return self._parse("/v1/ledgers", schemas.parse_envelope, payload, "ledgers", schemas.LedgerEntry)
