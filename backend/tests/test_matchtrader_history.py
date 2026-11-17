"""Older-history import and analytics for Match-Trader API connections.
Synthetic trades only; the broker is a scripted fake. Needs TALITRADE_TEST_DATABASE_URL."""
import time
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from app.services.matchtrader_api import config, engine as sync_engine, ratelimit, read_models, worker
from tests.mt_fakes import TOKEN_A, TOKEN_B, closed
from tests.test_matchtrader_api_db import ALICE, BOB, P, auth, connect, ctx, pytestmark, q, run_db  # noqa: F401

DAY = 86_400_000


async def drain(c, rounds=30):
    """Let the worker run until no requested history is left (the browser is closed)."""
    for _ in range(rounds):
        async with c.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET next_history_sync_at = NOW(), next_live_sync_at = NOW()"))
        ratelimit._budgets.clear()
        await worker.run_due_once(c.store, client_factory=c.factory)
        (left,), = await q(c, "SELECT COUNT(*) FROM mtapi_history_intervals WHERE status = 'queued' AND kind = 'backfill'")
        if not left:
            return
    raise AssertionError("history did not finish")


def history_over(days, now, every_hours=20):
    """Closed entries spread over ``days``; every 5th position is closed in two parts."""
    rows, n = [], 0
    for i in range(int(days * 24 / every_hours)):
        t = now - 3_600_000 - i * every_hours * 3_600_000
        if i % 5 == 0:
            rows += [closed(f"u{n}a", f"P{i}", t, net="5.5"), closed(f"u{n}b", f"P{i}", t - 60_000, net="-2")]
        else:
            rows.append(closed(f"u{n}", f"P{i}", t, net="3" if i % 2 else "-1.25"))
        n += 1
    return rows


def test_older_history_is_requested_incrementally_imported_once_and_never_duplicated(ctx, monkeypatch):
    monkeypatch.setenv("TALITRADE_MT_API_INITIAL_BACKFILL_DAYS", "7")

    async def scenario():
        now = int(time.time() * 1000)
        account = ctx.broker.accounts[TOKEN_A]
        account["account"]["createTime"] = now - 200 * DAY                       # the broker reports when the account was opened
        account["closed"] = history_over(200, now)
        account["ledgers"] = [{"id": "L1", "type": "DEPOSIT", "amount": 10000.0, "timestamp": now - 199 * DAY, "comment": ""}]
        cid = (await connect(ctx)).json()["connection"]["id"]
        (created,), = await q(ctx, "SELECT EXTRACT(EPOCH FROM account_created_at) * 1000 FROM mtapi_connections")
        assert abs(float(created) - (now - 200 * DAY)) < 2
        await drain(ctx)
        recent = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]
        assert recent["coverage_status"] == "complete_for_requested_range" and recent["closed_entry_count"] < 15   # only the first 7 days so far
        ctx.owners["700001"] = {ALICE}
        async with ctx.db.begin() as conn:                                        # legacy rows and their annotations are not touched
            await conn.execute(text("INSERT INTO trades (account_id, symbol, direction, volume, close_price, pnl, closed_at, source) VALUES ('700001','US30','buy',0.1,1,1,NOW(),'scraper')"))

        thirty = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "30d"})).json()
        assert thirty["queued"] == ["closed_positions", "ledgers"] and thirty["lower_bound"] == "30d"
        rows = await q(ctx, "SELECT range_from, range_to FROM mtapi_history_intervals WHERE kind = 'backfill' AND resource = 'closed_positions' AND status = 'queued'")
        (seven_from,), = await q(ctx, "SELECT MIN(range_from) FROM mtapi_history_intervals WHERE kind = 'backfill' AND resource = 'closed_positions' AND status <> 'queued'")
        assert len(rows) == 1 and rows[0][1] == seven_from + 1                     # only the 7 -> 30 day part is new, 1 ms overlap
        again = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "30d"})).json()
        assert again["queued"] == [] and again["already_requested"] == ["closed_positions", "ledgers"]
        assert (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "90d"})).json()["queued"] == ["closed_positions", "ledgers"]
        everything = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "all"})).json()
        assert everything["lower_bound"] == "account_created_at" and everything["queued"] == ["closed_positions", "ledgers"]
        assert "not known" in everything["note"] and everything["account_created_at"]
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]
        assert snap["coverage_status"] == "in_progress"
        await drain(ctx)
        snap = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]
        assert snap["coverage_status"] == "complete_for_requested_range" and snap["account_created_at"]
        assert snap["requested_from"] <= everything["requested_from"]
        (total, distinct, positions), = await q(ctx, "SELECT COUNT(*), COUNT(DISTINCT uid), COUNT(DISTINCT position_id) FROM mtapi_closed_positions")
        assert total == distinct == len(account["closed"])                        # every entry, exactly once
        assert positions == len({r["id"] for r in account["closed"]})              # partial closes keep their shared position id
        assert (await q(ctx, "SELECT MAX(revision) FROM mtapi_closed_positions"))[0][0] == 1   # re-reads of overlaps changed nothing
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_ledgers"))[0][0] == 1
        assert (await q(ctx, "SELECT COUNT(*) FROM trades"))[0][0] == 1

        # Requests after everything is done queue nothing and keep every row.
        for body in ({"range": "all"}, {"range": "30d"}, {"range": "custom", "from_ms": now - 100 * DAY}):
            assert (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json=body)).json()["queued"] == []
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_closed_positions"))[0][0] == total
        # Earlier than the account existed: clamped to the creation time, nothing new to fetch.
        early = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"from_ms": now - 400 * DAY})).json()
        assert early["clamped_to_account_creation"] is True and early["queued"] == []

        # New trades keep arriving automatically with the browser closed.
        account["closed"].append(closed("fresh-1", "PN", int(time.time() * 1000) - 5_000, net="7"))
        await drain(ctx)
        async with ctx.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET next_history_sync_at = NOW()"))
        await worker.run_due_once(ctx.store, client_factory=ctx.factory)
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_closed_positions WHERE uid = 'fresh-1'"))[0][0] == 1

    run_db(ctx, scenario)


def test_all_history_needs_a_start_date_when_the_creation_time_is_unknown_and_inputs_are_checked(ctx):
    async def scenario():
        now = int(time.time() * 1000)
        account = ctx.broker.accounts[TOKEN_A]
        account["account"]["createTime"] = None                                  # the broker does not say
        account["closed"] = [closed("old", "P1", now - 40 * DAY), closed("new", "P2", now - DAY)]
        cid = (await connect(ctx)).json()["connection"]["id"]
        res = await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "all"})
        assert res.status_code == 409 and res.json()["detail"]["reason"] == "start_date_required"
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_intervals"))[0][0] == 0   # no unbounded search was started
        for body, code in (({"range": "custom"}, 422), ({"range": "forever"}, 422), ({"from_ms": now + DAY}, 422), ({"from_ms": 5}, 422), ({}, 422)):
            assert (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json=body)).status_code == code, body
        ok = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "custom", "from_ms": now - 60 * DAY})).json()
        assert ok["lower_bound"] == "start_date" and ok["queued"] == ["closed_positions", "ledgers"] and ok["account_created_at"] is None
        await drain(ctx)
        assert {r[0] for r in await q(ctx, "SELECT uid FROM mtapi_closed_positions")} == {"old", "new"}
        # Another user cannot ask for this account's history, whatever they send.
        res = await ctx.http.post(f"{P}/{cid}/backfill", headers={**auth(BOB), "X-User-Id": ALICE}, json={"range": "30d", "user_id": ALICE})
        assert res.status_code == 404

    run_db(ctx, scenario)


def test_creation_time_is_looked_up_once_for_connections_stored_before_it_was_recorded(ctx):
    async def scenario():
        now = int(time.time() * 1000)
        ctx.broker.accounts[TOKEN_A]["account"]["createTime"] = now - 50 * DAY
        cid = (await connect(ctx)).json()["connection"]["id"]
        async with ctx.db.begin() as conn:
            await conn.execute(text("UPDATE mtapi_connections SET account_created_at = NULL"))   # as for a connection made earlier
        sync_engine._creation_checked.clear()
        before = len(ctx.broker.calls("/v1/account"))
        for _ in range(3):
            await sync_engine.sync_connection(ctx.store, cid, client_factory=ctx.factory, live=False, history=True)
        assert len(ctx.broker.calls("/v1/account")) == before + 1                # asked once, not every pass
        (created,), = await q(ctx, "SELECT account_created_at IS NOT NULL FROM mtapi_connections")
        assert created is True
        assert (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "all"})).json()["lower_bound"] == "account_created_at"

    run_db(ctx, scenario)


def test_an_interrupted_older_import_resumes_without_gaps_or_duplicates(ctx, monkeypatch):
    async def scenario():
        now = int(time.time() * 1000)
        account = ctx.broker.accounts[TOKEN_A]
        account["closed"] = [closed(f"u{i}", f"P{i // 2}", now - 10 * DAY - i * 3_600_000) for i in range(40)]
        cid = (await connect(ctx)).json()["connection"]["id"]
        await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "30d"})
        ctx.broker.fail["/v1/closed-positions"] = (503, {})                        # the broker drops out half way
        for _ in range(2):
            await sync_engine.sync_connection(ctx.store, cid, client_factory=ctx.factory, live=False, history=True)
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_closed_positions"))[0][0] == 0
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_history_intervals WHERE kind = 'backfill' AND status = 'queued'"))[0][0] >= 1
        ctx.broker.fail.clear()
        await drain(ctx)
        assert (await q(ctx, "SELECT COUNT(*), COUNT(DISTINCT uid) FROM mtapi_closed_positions"))[0] == (40, 40)
        empty = (await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "90d"})).json()
        assert empty["queued"] == ["closed_positions", "ledgers"]
        await drain(ctx)                                                          # an empty older range completes cleanly
        history = (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["history"]
        assert history["coverage_status"] == "complete_for_requested_range" and history["closed_entry_count"] == 40

    run_db(ctx, scenario)


# ── analytics ────────────────────────────────────────────────────────────────
async def insert_entries(c, cid, rows):
    """rows: (uid, position_id, close_iso, net, commission, swap, symbol)."""
    async with c.db.begin() as conn:
        for uid, pid, at, net, commission, swap, symbol in rows:
            await conn.execute(text("""
                INSERT INTO mtapi_closed_positions (connection_id, uid, position_id, symbol, side, volume, net_profit, profit, commission, swap, close_time, raw)
                VALUES (:id, :uid, :pid, :sym, 'BUY', 0.1, :net, :net, :com, :swap, CAST(:at AS timestamptz), '{}'::jsonb)
            """), {"id": cid, "uid": uid, "pid": pid, "sym": symbol, "net": net, "com": commission, "swap": swap,
                   "at": datetime.fromisoformat(at.replace("Z", "+00:00"))})
        await conn.execute(text("""INSERT INTO mtapi_sync_state (connection_id, resource, last_success_at) VALUES (:id, 'closed_positions', NOW())
                                   ON CONFLICT (connection_id, resource) DO UPDATE SET last_success_at = NOW()"""), {"id": cid})


def test_analytics_cover_every_stored_entry_with_stated_rules(ctx):
    async def scenario():
        ctx.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        alice = (await connect(ctx, ALICE, TOKEN_A)).json()["connection"]["id"]
        bob = (await connect(ctx, BOB, TOKEN_B)).json()["connection"]["id"]
        rows = []
        for i in range(230):                                                     # more than two pages of trades (page size 100)
            net = ["12.5", "-4.25", "0"][i % 3]
            rows.append((f"e{i}", f"P{i}", f"2026-10-{1 + i % 28:02d}T12:00:00Z", net, "-0.5", "-0.1", "EURUSD" if i % 2 else "XAUUSD"))
        rows += [("pa", "PP", "2026-11-10T07:30:00Z", "5", "-1", "0", "US30"),        # one position closed in two parts
                 ("pb", "PP", "2026-11-10T08:30:00Z", "-2", "-1", "0", "US30"),
                 ("nores", "PN", "2026-11-10T09:00:00Z", None, "0", "0", "US30")]      # no net result reported
        await insert_entries(ctx, alice, rows)
        await insert_entries(ctx, bob, [("b1", "B1", "2026-10-05T12:00:00Z", "1000", "0", "0", "EURUSD")])
        async with ctx.db.begin() as conn:                                        # a cash adjustment is not a trade result
            await conn.execute(text("INSERT INTO mtapi_ledgers (connection_id, external_id, entry_type, amount, entry_time, raw) VALUES (:id, 'L1', 'CLOSE_POSITION', 999, NOW(), '{}'::jsonb)"), {"id": alice})
        page = (await ctx.http.get(f"{P}/{alice}/trades?limit=100", headers=auth(ALICE))).json()
        assert len(page["trades"]) == 100 and page["has_more"] is True

        res = await ctx.http.get(f"{P}/{alice}/analytics", headers=auth(ALICE))
        body = res.json()
        assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
        s = body["summary"]
        wins, losses, evens = 77, 77, 76                                          # i % 3 == 0, 1, 2 over 230
        assert s["closed_entries"] == 233 and s["entries_with_result"] == 232 and s["entries_without_result"] == 1
        assert (s["winning_entries"], s["losing_entries"], s["breakeven_entries"]) == (wins + 1, losses + 1, evens)
        net = Decimal("12.5") * wins - Decimal("4.25") * losses + 5 - 2
        assert Decimal(s["net_profit"]) == net                                    # net results only: commission is NOT subtracted again
        assert Decimal(s["commission_total"]) == Decimal("-0.5") * 230 - 2 and Decimal(s["swap_total"]) == Decimal("-23")
        assert s["entry_win_rate_percent"] == str((Decimal(wins + 1) * 100 / 232).quantize(Decimal("0.1")))     # breakevens count as not winning
        assert s["positions"] == 232 and s["winning_positions"] == wins + 1 and s["losing_positions"] == losses and s["breakeven_positions"] == evens
        assert s["profit_factor"] == str(((Decimal("12.5") * wins + 5) / (Decimal("4.25") * losses + 2)).quantize(Decimal("0.01")))
        assert s["average_win"] == str(((Decimal("12.5") * wins + 5) / (wins + 1)).quantize(Decimal("0.01")))
        assert s["average_loss"] == str((-(Decimal("4.25") * losses + 2) / (losses + 1)).quantize(Decimal("0.01")))
        assert body["best_symbol"]["symbol"] in ("EURUSD", "XAUUSD")
        assert Decimal(body["cumulative_realized"][-1]["cumulative_net"]) == net and "not an account-equity" in body["counting_rules"]["cumulative"]
        assert "CLOSE_POSITION" in body["counting_rules"]["excluded"] and "drawdown" in body["not_derived"]
        assert body["coverage"]["may_change"] is True and body["coverage"]["status"] == "not_requested"   # rows without a requested range are never "complete"

        # Day boundaries follow the stated time zone.
        utc = {d["day"]: d for d in body["daily"]}
        assert utc["2026-11-10"]["entries"] == 3 and "2026-11-09" not in utc
        la = {d["day"]: d for d in (await ctx.http.get(f"{P}/{alice}/analytics?tz=America/Los_Angeles", headers=auth(ALICE))).json()["daily"]}
        assert la["2026-11-09"]["entries"] == 1 and la["2026-11-09"]["net_profit"] == "5" and la["2026-11-10"]["entries"] == 2   # 07:30Z is Nov 9 in Los Angeles

        # A range: [from, to) on close time.
        nov = (await ctx.http.get(f"{P}/{alice}/analytics?from_ms=1794294000000&to_ms=1794301200000", headers=auth(ALICE))).json()
        assert nov["summary"]["closed_entries"] == 2 and nov["summary"]["net_profit"] == "3"    # 07:30 and 08:30 on Nov 10; 09:00 is excluded
        none = (await ctx.http.get(f"{P}/{alice}/analytics?from_ms=1000&to_ms=2000", headers=auth(ALICE))).json()["summary"]
        assert none["closed_entries"] == 0 and none["entry_win_rate_percent"] is None and none["profit_factor"] is None
        assert none["profit_factor_undefined_reason"] == "no_results" and none["average_win"] is None
        for bad in ("tz=Mars/Base", "from_ms=10&to_ms=5", "tz=" + "A" * 70):
            assert (await ctx.http.get(f"{P}/{alice}/analytics?{bad}", headers=auth(ALICE))).status_code == 422, bad

        # Each account stands alone: no blending, no reading someone else's.
        b = (await ctx.http.get(f"{P}/{bob}/analytics", headers=auth(BOB))).json()["summary"]
        assert b["closed_entries"] == 1 and b["net_profit"] == "1000" and b["profit_factor"] is None and b["profit_factor_undefined_reason"] == "no_losses"
        for path in (f"{P}/{bob}/analytics", f"{P}/{bob}/analytics?tz=UTC&user_id={ALICE}"):
            assert (await ctx.http.get(path, headers={**auth(ALICE), "X-User-Id": BOB})).status_code == 404
        assert (await ctx.http.get(f"{P}/{alice}/analytics")).status_code == 401

    run_db(ctx, scenario)


def test_analytics_report_partial_coverage_while_older_history_imports(ctx, monkeypatch):
    async def scenario():
        now = int(time.time() * 1000)
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed(f"u{i}", f"P{i}", now - i * DAY - 60_000, net="2") for i in range(40)]
        cid = (await connect(ctx)).json()["connection"]["id"]
        await ctx.http.post(f"{P}/{cid}/backfill", headers=auth(ALICE), json={"range": "30d"})
        monkeypatch.setattr(config, "history_pass_seconds", lambda: 0)
        await sync_engine.sync_connection(ctx.store, cid, client_factory=ctx.factory, live=False, history=True)
        partial = (await ctx.http.get(f"{P}/{cid}/analytics", headers=auth(ALICE))).json()
        assert partial["coverage"]["status"] == "in_progress" and partial["coverage"]["may_change"] is True
        before = partial["summary"]["closed_entries"]
        monkeypatch.setattr(config, "history_pass_seconds", lambda: 12)
        await drain(ctx)
        done = (await ctx.http.get(f"{P}/{cid}/analytics", headers=auth(ALICE))).json()
        assert done["coverage"]["status"] == "complete_for_requested_range" and done["coverage"]["may_change"] is False
        assert done["summary"]["closed_entries"] >= before and done["summary"]["closed_entries"] == 30   # one per day inside the 30 days; values grew as history arrived
        wider = (await ctx.http.get(f"{P}/{cid}/analytics?from_ms={now - 60 * DAY}", headers=auth(ALICE))).json()
        assert wider["coverage"]["range_starts_before_requested_history"] is True        # the user is told the range is wider than the import
        assert read_models.analytics_summary({"entries": 0, "wins": 0, "losses": 0, "breakeven": 0, "gross_win": 0, "gross_loss": 0, "net": 0, "positions": 0,
                                              "commission": 0, "swap": 0, "first_close": None, "last_close": None},
                                             {"wins": 0, "losses": 0, "breakeven": 0})["position_win_rate_percent"] is None

    run_db(ctx, scenario)


def test_older_history_requested_during_a_history_pass_still_starts_right_away(ctx):
    """A pass that is finishing must not push a just-requested import back by a whole history interval."""
    async def scenario():
        import httpx
        now = int(time.time() * 1000)
        ctx.broker.accounts[TOKEN_A]["closed"] = [closed(f"u{i}", f"P{i}", now - 20 * DAY - i * 3_600_000) for i in range(5)]
        cid = (await connect(ctx)).json()["connection"]["id"]
        requested = {}

        class RequestMidPass(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.path == "/v1/closed-positions" and not requested:   # the user clicks "30 days" while history runs
                    requested["result"] = await ctx.store.request_older_history(cid, ALICE, now - 30 * DAY, now, ["closed_positions", "ledgers"])
                return ctx.broker.handler(request)

        factory = lambda token, budget=None: sync_engine.api.MatchTraderApiClient(token, budget=budget, transport=RequestMidPass())   # noqa: E731
        await sync_engine.sync_connection(ctx.store, cid, client_factory=factory, live=False, history=True, schedule=True)
        assert requested["result"] and requested["result"][0]["queued"] is True
        (wait,), = await q(ctx, "SELECT EXTRACT(EPOCH FROM next_history_sync_at - NOW()) FROM mtapi_connections")
        assert float(wait) <= 1, wait                                             # due now, not after the 120 s history interval
        await worker.run_due_once(ctx.store, client_factory=ctx.factory)
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_closed_positions"))[0][0] == 5

    run_db(ctx, scenario)


def test_pilot_readiness_stays_with_the_pilot_user_on_every_route(ctx, monkeypatch):
    """Production's pilot gate: connections closed in general, open for one verified user only."""
    monkeypatch.setenv("TALITRADE_MT_API_CONNECT_ENABLED", "false")
    monkeypatch.setenv("TALITRADE_MT_API_PILOT_USER_IDS", ALICE)
    monkeypatch.setenv("TALITRADE_MT_API_MARKET_DATA_ENABLED", "true")

    async def scenario():
        ctx.broker.add_account(TOKEN_B, login="800002", platform_url="https://mtr.broker-one.example")
        alice_cards = (await ctx.http.get(f"{P}?view=cards", headers=auth(ALICE))).json()
        bob_cards = (await ctx.http.get(f"{P}?view=cards", headers=auth(BOB))).json()
        assert alice_cards["service"]["accepting_connections"] is True and bob_cards["service"]["accepting_connections"] is False
        before = len(ctx.broker.requests)
        refused = await connect(ctx, BOB, TOKEN_B)
        assert refused.status_code == 503 and len(ctx.broker.requests) == before        # Bob's token is never even used
        cid = (await connect(ctx, ALICE, TOKEN_A)).json()["connection"]["id"]
        assert (await ctx.http.get(f"{P}/{cid}/snapshot", headers=auth(ALICE))).json()["service"]["accepting_connections"] is True
        for method, path, body in (("GET", "market/ticker", None), ("GET", "market/candles", None), ("GET", "analytics", None),
                                   ("POST", "backfill", {"range": "30d"}), ("GET", "snapshot", None)):
            res = await ctx.http.request(method, f"{P}/{cid}/{path}", headers=auth(BOB), json=body)
            assert res.status_code == 404, path                                       # pilot status grants nothing on someone else's account
            assert (await ctx.http.request(method, f"{P}/{cid}/{path}", headers=auth(ALICE), json=body)).status_code == 200, path
        assert (await q(ctx, "SELECT COUNT(*) FROM mtapi_connections"))[0][0] == 1

    run_db(ctx, scenario)
