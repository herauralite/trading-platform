"""Match-Trader API integration: tests that need no database."""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core.auth_session import create_session_token  # noqa: E402
from app.main import app  # noqa: E402
from app.services.matchtrader_api import backfill, client as api, config, crypto, ratelimit, reconcile, routes, schemas  # noqa: E402
from tests.mt_fakes import TOKEN_A, FakeBroker, closed, configure_keys, position  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_client(broker, token=TOKEN_A, budget=None):
    return api.MatchTraderApiClient(token, budget=budget, transport=broker.transport())


# ── flags ───────────────────────────────────────────────────────────────────
def test_integration_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TALITRADE_MT_API_ENABLED", raising=False)
    monkeypatch.setenv("TALITRADE_MT_API_WORKER_ENABLED", "true")
    assert config.api_enabled() is False and config.worker_enabled() is False  # worker needs the main flag too

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            headers = {"Authorization": f"Bearer {create_session_token('1001')}"}
            return [(await http.get("/connectors/matchtrader-api/connections", headers=headers)).status_code,
                    (await http.post("/connectors/matchtrader-api/connections", headers=headers, json={"token": "x" * 40})).status_code,
                    (await http.delete("/connectors/matchtrader-api/connections/1", headers=headers)).status_code]

    assert run(go()) == [404, 404, 404]


def test_routes_require_a_user_session_not_an_extension_token(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "true")

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            out = []
            for headers in ({}, {"Authorization": "Bearer tti_some-extension-token"}, {"Authorization": "Bearer junk"}):
                out.append((await http.get("/connectors/matchtrader-api/connections", headers=headers)).status_code)
                out.append((await http.post("/connectors/matchtrader-api/connections", headers=headers, json={"token": "x" * 40})).status_code)
                out.append((await http.post("/connectors/matchtrader-api/connections/1/sync", headers=headers)).status_code)
            return out

    assert set(run(go())) == {401}


def test_token_never_appears_in_validation_errors(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_ENABLED", "true")

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            headers = {"Authorization": f"Bearer {create_session_token('1001')}"}
            return await http.post("/connectors/matchtrader-api/connections", headers=headers, json={"token": "short-secret"})

    res = run(go())
    assert res.status_code == 422 and "short-secret" not in res.text
    assert "short-secret" not in repr(routes.TokenBody(token="s" * 30 + "short-secret"))
    assert "min_length" not in str(routes.TokenBody.model_json_schema())   # no constraint that would echo input


# ── client ──────────────────────────────────────────────────────────────────
def test_client_is_get_only_fixed_host_and_path_allowlisted():
    broker = FakeBroker()

    async def go():
        async with make_client(broker) as c:
            await c.account()
            await c.positions()
            for bad in ("/v1/positions/close", "/v1/orders", "https://evil.example/v1/account", "/v1/account/../orders"):
                with pytest.raises(ValueError):
                    await c._get(bad)
            assert not hasattr(c, "post") and not hasattr(c, "close_position")
            assert str(c._http.base_url) == "https://trader-api.match-trader.com"
            assert c._http.follow_redirects is False

    run(go())
    assert {r[0] for r in broker.requests} == {"GET"} and len(broker.requests) == 2
    assert api.BASE_URL == "https://trader-api.match-trader.com"
    assert all(p.startswith("/v1/") for p in api.ALLOWED_PATHS) and len(api.ALLOWED_PATHS) == 7


def test_client_never_follows_redirects_with_the_token():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    async def go():
        async with api.MatchTraderApiClient(TOKEN_A, transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(api.ContractError) as excinfo:
                await c.account()
            return excinfo.value

    err = run(go())
    assert err.problem_type == "unexpected_redirect" and len(seen) == 1 and "evil" not in seen[0]


@pytest.mark.parametrize("status,expected", [(401, api.Unauthorized), (403, api.Forbidden), (429, api.RateLimited), (500, api.TransientError),
                                              (503, api.TransientError), (400, api.RequestRejected), (404, api.RequestRejected)])
def test_client_maps_status_codes_and_ignores_detail_prose(status, expected):
    broker = FakeBroker()
    broker.fail["/v1/positions"] = (status, {"Retry-After": "7"})

    async def go():
        async with make_client(broker) as c:
            with pytest.raises(expected) as excinfo:
                await c.positions()
            return excinfo.value

    err = run(go())
    assert err.status == status and err.problem_type.endswith(str(status))
    assert "prose" not in str(err) and TOKEN_A not in str(err) and TOKEN_A not in repr(err)
    if status == 429:
        assert err.retry_after == 7.0


def test_errors_are_never_reported_as_empty_data():
    def handler(request):
        if request.url.path == "/v1/positions":
            return httpx.Response(200, json={"items": []})          # wrong envelope
        if request.url.path == "/v1/pending-orders":
            return httpx.Response(200, content=b"<html>gateway</html>")  # not JSON
        if request.url.path == "/v1/account-summary":
            return httpx.Response(200, json={"balance": "abc", "equity": 1})
        raise httpx.ConnectTimeout("boom")

    async def go():
        async with api.MatchTraderApiClient(TOKEN_A, transport=httpx.MockTransport(handler)) as c:
            for call, kind in ((c.positions, api.ContractError), (c.pending_orders, api.ContractError), (c.account_summary, api.ContractError), (c.account, api.TransientError)):
                with pytest.raises(kind):
                    await call()

    run(go())


def test_oversized_response_is_rejected():
    def handler(request):
        return httpx.Response(200, content=b'{"positions":[' + b" " * (api.MAX_RESPONSE_BYTES + 10) + b"]}")

    async def go():
        async with api.MatchTraderApiClient(TOKEN_A, transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(api.ContractError) as excinfo:
                await c.positions()
            return excinfo.value.problem_type

    assert run(go()) == "response_too_large"


def test_token_is_not_logged_or_exposed(caplog):
    broker = FakeBroker()
    broker.fail["/v1/account"] = (500, {})

    async def go():
        async with make_client(broker) as c:
            assert TOKEN_A not in repr(c) and TOKEN_A not in str(vars(c))
            with pytest.raises(api.TransientError):
                await c.account()
        async with api.MatchTraderApiClient(TOKEN_A, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as bad:
            with pytest.raises(api.ContractError):
                await bad.positions()

    with caplog.at_level(logging.DEBUG):
        run(go())
    assert TOKEN_A not in caplog.text
    with pytest.raises(ValueError):
        api.MatchTraderApiClient("has space")


# ── schemas ─────────────────────────────────────────────────────────────────
def test_schema_keeps_ids_as_strings_decimals_exact_and_nulls_null():
    entry = schemas.ClosedPosition.model_validate({**closed(123456789012345678901, position_id=42), "side": None, "netProfit": 0.1, "volume": 0.07,
                                                   "closeReason": "SOME_NEW_REASON", "brandNewField": {"x": 1}, "slPrice": None})
    assert entry.uid == "123456789012345678901" and entry.id == "42"
    assert entry.netProfit == Decimal("0.1") and entry.volume == Decimal("0.07") and entry.netProfit + Decimal("0.2") == Decimal("0.3")
    assert entry.side is None and entry.slPrice is None and entry.closeReason == "SOME_NEW_REASON"   # unknown enum kept, never coerced
    assert entry.raw["brandNewField"] == {"x": 1}
    order = schemas.PendingOrder.model_validate({"id": "9", "symbol": "EURUSD", "volume": 1, "side": None, "type": None})
    assert order.side is None and order.type is None and order.activationPrice is None
    when = schemas.ms_to_datetime(1_700_000_000_123)
    assert when == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=timezone.utc) and schemas.datetime_to_ms(when) == 1_700_000_000_123
    summary = schemas.AccountSummary.model_validate({"balance": 10000, "equity": 10000, "margin": 0, "freeFunds": 10000, "marginLevel": 0})
    assert summary.freeFunds == Decimal("10000") and summary.marginLevel == Decimal("0")  # zero margin is a value, not "missing"
    profile = schemas.AccountProfile.model_validate({"login": 700001, "email": "x@y.z", "firstName": "A", "accessRight": "WHATEVER_NEW"})
    assert profile.login == "700001" and not hasattr(profile, "email")   # PII not modelled
    with pytest.raises(schemas.ContractViolation):
        schemas.parse_envelope({"closedPositions": [{"uid": "1"}]}, "closedPositions", schemas.ClosedPosition)
    with pytest.raises(schemas.ContractViolation):
        schemas.parse_envelope([], "positions", schemas.OpenPosition)
    assert schemas.parse_envelope({"positions": []}, "positions", schemas.OpenPosition) == []
    assert schemas.OpenPosition.model_validate(position("7")).id == "7"


# ── crypto ──────────────────────────────────────────────────────────────────
def test_token_encryption_binds_owner_and_supports_key_rotation(monkeypatch):
    configure_keys(monkeypatch)
    aad = crypto.build_associated_data(user_id="1001", connection_id=5, platform_host="h.example", external_login="700001")
    blob = crypto.encrypt_token(TOKEN_A, aad)
    assert TOKEN_A not in blob and blob.startswith("v1.") and crypto.decrypt_token(blob, aad) == TOKEN_A
    assert crypto.encrypt_token(TOKEN_A, aad) != blob  # fresh nonce every time
    for other in (dict(user_id="2002", connection_id=5), dict(user_id="1001", connection_id=6)):
        wrong = crypto.build_associated_data(platform_host="h.example", external_login="700001", **other)
        with pytest.raises(crypto.TokenCryptoError) as excinfo:
            crypto.decrypt_token(blob, wrong)   # ciphertext moved to another owner/row does not open
        assert TOKEN_A not in str(excinfo.value)
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt_token(tampered, aad)
    # add v2 and make it active: old ciphertext still opens, new writes use v2
    keys = json.loads(os.environ["TALITRADE_MT_TOKEN_KEYS"])
    import base64
    keys["v2"] = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("TALITRADE_MT_TOKEN_KEYS", json.dumps(keys))
    monkeypatch.setenv("TALITRADE_MT_TOKEN_KEY_ACTIVE", "v2")
    assert crypto.decrypt_token(blob, aad) == TOKEN_A and crypto.key_version_of(crypto.encrypt_token(TOKEN_A, aad)) == "v2"
    # keys missing or the session secret alone are not enough
    monkeypatch.delenv("TALITRADE_MT_TOKEN_KEYS")
    assert crypto.is_configured() is False
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt_token(blob, aad)
    assert crypto.mask_token(TOKEN_A) == "...a1b2" and crypto.mask_token("short") == "..."


# ── history planning ────────────────────────────────────────────────────────
def test_history_planner_rules():
    assert backfill.evaluate(0, 10_000, 499) == backfill.COMPLETE
    assert backfill.evaluate(0, 10_000, 500) == backfill.SPLIT       # exactly 500 is treated as possibly truncated
    assert backfill.evaluate(0, 2, 500) == backfill.POINTS and backfill.evaluate(5, 5, 500) == backfill.GAP
    assert backfill.points(7, 9) == [(7, 7), (8, 8), (9, 9)]
    with pytest.raises(ValueError):
        backfill.split(0, 2)
    for lo, hi in ((0, 3), (0, 1000), (1_600_000_000_000, 1_700_000_000_000), (10, 17)):
        a, b = backfill.split(lo, hi)
        assert a[0] == lo and b[1] == hi and a[1] > b[0]            # covers parent, halves overlap
        assert a[1] - a[0] < hi - lo and b[1] - b[0] < hi - lo       # both strictly smaller: always terminates


def simulate(times, limit=500, inclusive=True):
    """Drive the planner the way engine.process_interval does, against a newest-first, capped
    endpoint. ``inclusive=False`` models a provider whose range bounds are exclusive."""
    rows = sorted(enumerate(times), key=lambda r: r[1], reverse=True)

    def fetch(lo, hi, cap):
        hit = (lambda t: lo <= t <= hi) if inclusive else (lambda t: lo < t < hi)
        return [(i, t) for i, t in rows if hit(t)][:cap]

    queue, got, gaps, requests = [(min(times) - 1, max(times) + 1)], set(), [], 0   # widened by 1 ms, like the engine
    while queue:
        lo, hi = queue.pop()
        page = fetch(lo, hi, limit)
        requests += 1
        got.update(i for i, _ in page)
        verdict = backfill.evaluate(lo, hi, len(page), limit)
        if verdict == backfill.SPLIT:
            queue.extend(backfill.split(lo, hi))
        elif verdict == backfill.POINTS:
            requests += 1
            if fetch(page[0][1], page[0][1], 1):     # in-situ proof that from == to is inclusive
                queue.extend(backfill.points(lo, hi))
            else:
                gaps.append((lo, hi))
        elif verdict == backfill.GAP:
            gaps.append((lo, hi))
    return got, gaps, requests


BASE = 1_700_000_000_000
TRAVERSAL_CASES = {
    "single row": [BASE],
    "exactly 500": [BASE + i for i in range(500)],
    "501": [BASE + i for i in range(501)],
    "2000 spread": [BASE + i * 37 for i in range(2000)],
    "adjacent dense milliseconds": [BASE] * 499 + [BASE + 1] * 499 + [BASE + 5000],
    "clustered": [BASE] * 300 + [BASE + 1] * 300 + [BASE + 2] * 300 + [BASE + 9_999_999] * 10,
}


@pytest.mark.parametrize("name", list(TRAVERSAL_CASES))
def test_history_traversal_is_complete_with_inclusive_bounds(name):
    times = TRAVERSAL_CASES[name]
    got, gaps, _ = simulate(times)
    assert got == set(range(len(times))) and gaps == []


@pytest.mark.parametrize("name", ["single row", "exactly 500", "501", "2000 spread"])
def test_history_traversal_is_complete_even_if_bounds_are_exclusive(name):
    times = TRAVERSAL_CASES[name]
    got, gaps, _ = simulate(times, inclusive=False)
    assert got == set(range(len(times))) and gaps == []


def test_adjacent_dense_milliseconds_are_complete_even_with_exclusive_bounds():
    times = TRAVERSAL_CASES["adjacent dense milliseconds"]
    got, gaps, _ = simulate(times, inclusive=False)
    assert got == set(range(len(times))) and gaps == []


@pytest.mark.parametrize("inclusive", [True, False])
def test_saturated_single_millisecond_is_reported_as_a_gap_not_as_complete(inclusive):
    got, gaps, requests = simulate([BASE] * 600 + [BASE + 60_000] * 5, inclusive=inclusive)
    assert gaps and all(lo <= BASE <= hi and hi - lo <= backfill.ATOMIC_SPAN_MS for lo, hi in gaps)
    assert 500 <= len(got) < 605 and requests < 200   # what was returned is kept; the rest is an explicit gap
    if inclusive:
        assert set(gaps) == {(BASE, BASE)}


# ── rate budget ─────────────────────────────────────────────────────────────
def test_rate_budget_headroom_priorities_headers_and_retry_after():
    now = [0.0]
    budget = ratelimit.AccountRateBudget(clock=lambda: now[0])
    path = "/v1/positions"   # documented 20/min -> 14 usable, 7 for backfill
    for _ in range(7):
        budget.reserve(path, low_priority=True)
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve(path, low_priority=True)      # backfill yields...
    for _ in range(7):
        budget.reserve(path)                         # ...live sync still has room
    with pytest.raises(ratelimit.BudgetExhausted) as excinfo:
        budget.reserve(path)
    assert 0 < excinfo.value.retry_in <= 60
    budget.reserve("/v1/ledgers")                    # endpoints are budgeted separately
    now[0] = 61.0
    budget.reserve(path)                             # sliding window frees up
    pause = budget.penalize(path, 20)                # 429 with Retry-After: 20
    assert 20 < pause <= 23.5
    now[0] += 19
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve(path)
    now[0] += 6
    budget.reserve(path)
    budget.observe_headers("/v1/ledgers", remaining_endpoint=0, remaining_global=50)
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve("/v1/ledgers")
    budget.observe_headers("/v1/account", remaining_endpoint=5, remaining_global=0)   # someone else spent the account budget
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve("/v1/account-summary")
    budget.observe_headers("/v1/account", None, None)  # missing headers are tolerated
    assert ratelimit.budget_for("h|1") is ratelimit.budget_for("h|1") and ratelimit.budget_for("h|1") is not ratelimit.budget_for("h|2")


def test_endpoint_scoped_429_pauses_only_that_endpoint():
    broker = FakeBroker()
    broker.fail["/v1/positions"] = (429, {"Retry-After": "30", "X-RateLimit-Remaining-Endpoint": "0", "X-RateLimit-Remaining-Global": "25"})
    budget = ratelimit.AccountRateBudget()

    async def go():
        async with make_client(broker, budget=budget) as c:
            with pytest.raises(api.RateLimited):
                await c.positions()
            for _ in range(5):
                with pytest.raises(ratelimit.BudgetExhausted):
                    await c.positions()
            await c.account_summary()   # headers proved the account still has budget: other endpoints continue

    run(go())
    assert len(broker.calls("/v1/positions")) == 1   # no retry storm


@pytest.mark.parametrize("headers", [{"Retry-After": "30"}, {"Retry-After": "30", "X-RateLimit-Remaining-Global": "0"},
                                     {"Retry-After": "30", "X-RateLimit-Remaining-Endpoint": "3", "X-RateLimit-Remaining-Global": "0"}])
def test_account_wide_or_ambiguous_429_pauses_every_endpoint(headers):
    broker = FakeBroker()
    broker.fail["/v1/positions"] = (429, headers)
    budget = ratelimit.AccountRateBudget()

    async def go():
        async with make_client(broker, budget=budget) as c:
            with pytest.raises(api.RateLimited):
                await c.positions()
            for call in (c.positions, c.account_summary, c.pending_orders, c.account):
                with pytest.raises(ratelimit.BudgetExhausted):
                    await call()

    run(go())
    assert len(broker.requests) == 1   # nothing else was sent to an exhausted account


def test_account_wide_budget_caps_total_requests_across_endpoints(monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_GLOBAL_PER_MINUTE", "12")
    now = [0.0]
    budget = ratelimit.AccountRateBudget(clock=lambda: now[0])
    paths = ["/v1/account-summary", "/v1/positions", "/v1/pending-orders", "/v1/closed-positions"]
    sent = 0
    for i in range(40):
        try:
            budget.reserve(paths[i % 4])
            sent += 1
        except ratelimit.BudgetExhausted:
            pass
    assert sent == 12                                   # no endpoint hit its own limit, the account cap did
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve("/v1/ledgers", low_priority=True)
    now[0] = 61.0
    for _ in range(6):
        budget.reserve("/v1/ledgers", low_priority=True)   # backfill may use half of the account budget
    with pytest.raises(ratelimit.BudgetExhausted):
        budget.reserve("/v1/ledgers", low_priority=True)
    budget.reserve("/v1/positions")                        # live sync still has the other half


def test_sync_outcome_is_interpreted_honestly():
    from app.services.matchtrader_api.read_models import interpret_sync_outcome as verdict
    ok = {"summary": "ok", "positions": "ok", "pending_orders": "ok", "closed_positions": "ok", "ledgers": "ok"}
    assert verdict(ok)["state"] == "ok" and verdict(ok)["live_ready"] is True
    assert verdict({**ok, "ledgers": "not_permitted"})["state"] == "ok"
    partial = verdict({**ok, "ledgers": "forbidden", "closed_positions": "transient"})
    assert partial["state"] == "partial" and partial["live_ready"] is True
    assert verdict({"skipped": "busy"}) == {"state": "busy", "live_ready": False, "resources": {}, "detail": "busy"}
    assert verdict({"skipped": "needs_reauth"})["state"] == "needs_reauth"
    assert verdict({"skipped": "paused"})["state"] == "not_active"
    assert verdict({"summary": "unauthorized"})["state"] == "needs_reauth"
    limited = verdict({"summary": "rate_limited", "positions": "rate_limited"})
    assert limited["state"] == "rate_limited" and limited["live_ready"] is False
    failed = verdict({"summary": "ok", "positions": "transient", "pending_orders": "ok"})
    assert failed["state"] == "failed" and failed["live_ready"] is False     # positions did not persist: not "synced"
    assert verdict({"summary": "fenced"})["state"] == "busy"
    assert verdict({})["live_ready"] is False


def test_trade_cursor_round_trips_and_rejects_tampering():
    from app.services.matchtrader_api import read_models as rm
    when = datetime(2026, 11, 14, 22, 0, 0, 123000, tzinfo=timezone.utc)
    cursor = rm.encode_cursor(when, "uid-9")
    assert rm.decode_cursor(cursor) == (when, "uid-9") and "=" not in cursor
    for bad in ("", "x", "not base64 !!", cursor[:-3], "W10", "eyJhIjoxfQ", "A" * 500,
                "WyIyMDI2LTExLTE0VDIyOjAwOjAwIiwgInUiXQ",                      # naive timestamp
                "WyIxOyBEUk9QIFRBQkxFIHRyYWRlcyIsICJ1Il0"):                     # SQL in the timestamp slot
        with pytest.raises(rm.BadCursor):
            rm.decode_cursor(bad)
    assert rm.mask_login("12340001") == "…0001" and rm.mask_login("123") == "…" and rm.account_key(12) == "mtapi:12"


# ── reconciliation proposal ─────────────────────────────────────────────────
def test_reconciliation_report_matches_exactly_flags_ambiguity_and_never_writes():
    t = datetime(2026, 4, 16, 10, 0, 0, 250000, tzinfo=timezone.utc)
    api_rows = [
        {"uid": "u1", "position_id": "P1", "symbol": "US30", "side": "BUY", "volume": Decimal("0.10"), "close_price": 1, "net_profit": Decimal("24.50"), "profit": Decimal("25"), "close_time": t},
        {"uid": "u2", "position_id": "P1", "symbol": "US30", "side": "BUY", "volume": Decimal("0.05"), "close_price": 1, "net_profit": Decimal("10"), "profit": Decimal("10"), "close_time": t},
        {"uid": "u3", "position_id": "P2", "symbol": "XAUUSD", "side": "SELL", "volume": Decimal("1"), "close_price": 1, "net_profit": Decimal("-5"), "profit": Decimal("-5"), "close_time": t},
        {"uid": "u4", "position_id": "P3", "symbol": "NAS100", "side": "BUY", "volume": Decimal("1"), "close_price": 1, "net_profit": Decimal("7"), "profit": Decimal("7"), "close_time": t},
    ]
    legacy = [
        {"id": 1, "symbol": "US30", "direction": "buy", "volume": 0.1, "close_price": 1, "pnl": 25.0, "closed_at": t.replace(microsecond=0), "source": "scraper"},
        {"id": 2, "symbol": "XAUUSD", "direction": "sell", "volume": 1.0, "close_price": 1, "pnl": -5.0, "closed_at": t.replace(microsecond=0), "source": "scraper"},
        {"id": 3, "symbol": "XAUUSD", "direction": "sell", "volume": 1.0, "close_price": 1, "pnl": -5.0, "closed_at": t.replace(microsecond=0), "source": "scraper"},
        {"id": 4, "symbol": "EURUSD", "direction": "buy", "volume": 1.0, "close_price": 1, "pnl": 3.0, "closed_at": t.replace(microsecond=0), "source": "scraper"},
    ]
    report = reconcile.build_report(api_rows, legacy)
    assert report["matched"] == 1 and report["matched_detail"][0] == {"uid": "u1", "legacy_trade_id": 1, "pnl_difference": "-0.50"}
    assert report["ambiguous"] == 1 and report["ambiguous_detail"][0]["legacy_trade_ids"] == [2, 3]   # never auto-merged
    assert sorted(report["api_only_uids"]) == ["u2", "u4"] and report["legacy_only_ids"] == [4]
    assert report["positions_closed_in_parts"] == 1 and report["writes_performed"] == 0
    assert report["api_net_profit_total"] == "36.50"   # netProfit only: commission/swap/ledgers are not added again
def test_pilot_readiness_is_owner_specific(monkeypatch):
    from app.services.matchtrader_api import config, crypto
    monkeypatch.setattr(crypto, "is_configured", lambda: True)
    for name in ("TALITRADE_MT_API_ENABLED", "TALITRADE_MT_API_WORKER_ENABLED"):
        monkeypatch.setenv(name, "true")
    monkeypatch.delenv("TALITRADE_MT_API_CONNECT_ENABLED", raising=False)
    monkeypatch.setenv("TALITRADE_MT_API_PILOT_USER_IDS", "123")
    assert config.service_status("123")["accepting_connections"]
    assert not config.service_status("124")["accepting_connections"]
    assert not config.service_status()["accepting_connections"]
    monkeypatch.setenv("TALITRADE_MT_API_WORKER_ENABLED", "false")
    assert not config.service_status("123")["accepting_connections"]
