"""Overview progress: exact calculation rules, on synthetic facts (no database).

Each test states the arithmetic it checks, so the definitions in progress.DEFINITIONS can be
verified by hand."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.matchtrader_api import progress as pg
from app.services.matchtrader_api.progress import Coverage, Facts

NY = pg.zone("America/New_York")
UTC = timezone.utc
NOW = datetime(2026, 11, 17, 20, 30, tzinfo=UTC)          # 15:30 in New York
CREATED = NOW - timedelta(days=200)


def ms(d):
    return int(d.timestamp() * 1000)


def cov(**kw):
    base = dict(permitted=True, synced_at=NOW - timedelta(seconds=30), stale=False, earliest_ms=ms(CREATED) - 1, gaps=[], queued=[])
    base.update(kw)
    return Coverage(**base)


def facts(profile=None, **kw):
    d, d0, d1 = pg.local_day(NOW, NY)
    f = Facts(now=NOW, currency="USD", account_created_at=CREATED, balance=Decimal("104200"), equity=Decimal("104200"), floating=Decimal("0"),
              summary_at=NOW - timedelta(seconds=10), positions=[], closed=cov(), ledgers=cov(),
              day={"net": Decimal("0"), "entries": 0, "positions": 0, "unvalued": 0, "last": None, "date": d, "start": d0, "end": d1},
              all_trades={"net": Decimal("4200"), "entries": 30, "positions": 30, "unvalued": 0, "last": None},
              cash=[{"id": "L0", "type": "DEPOSIT", "amount": Decimal("100000"), "at": CREATED}], profile=profile, setup_enabled=True)
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def money(v, unit="amount", base="starting"):
    return {"value": str(v), "unit": unit, "base": base}


def evaluation(phase_count=2, current=1, target=money(10, "percent"), **extra):
    ev = {"phaseCount": phase_count, "phaseTarget": target}
    if current:
        ev["currentPhase"] = current
    ev.update(extra)
    return {"status": "saved", "type": "evaluation", "size": {"amount": "100000", "currency": "USD"}, "evaluation": ev}


def master(**m):
    return {"status": "saved", "type": "master", "size": {"amount": "100000", "currency": "USD"}, "master": m}


def row(out, rid):
    return next(r for r in out["rows"] if r["id"] == rid)


# ───────── calendar days and resets ─────────
def test_dashboard_day_has_its_true_length_across_daylight_saving():
    fall = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)          # New York falls back that night (25 hours)
    spring = datetime(2026, 3, 8, 15, 0, tzinfo=UTC)         # springs forward (23 hours)
    for when, hours in ((fall, 25), (spring, 23), (NOW, 24)):
        _, a, b = pg.local_day(when, NY)
        assert (b - a) == timedelta(hours=hours)
        assert a.astimezone(NY).hour == 0 and b.astimezone(NY).hour == 0


def test_midnight_rollover_moves_to_the_next_day():
    before = datetime(2026, 11, 18, 4, 59, 59, tzinfo=UTC)   # 23:59:59 New York, Nov 17
    after = datetime(2026, 11, 18, 5, 0, 0, tzinfo=UTC)      # 00:00:00 New York, Nov 18
    assert pg.local_day(before, NY)[0].isoformat() == "2026-11-17"
    assert pg.local_day(after, NY)[0].isoformat() == "2026-11-18"
    assert pg.local_day(before, NY)[2] == pg.local_day(after, NY)[1]


def test_reset_time_is_the_last_one_at_or_before_now_in_its_own_zone():
    start, nxt = pg.last_reset(NOW, NY, "17:00")              # 15:30 NY -> yesterday 17:00
    assert start == datetime(2026, 11, 16, 22, 0, tzinfo=UTC) and nxt == datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
    start, _ = pg.last_reset(datetime(2026, 11, 17, 22, 0, tzinfo=UTC), NY, "17:00")
    assert start == datetime(2026, 11, 17, 22, 0, tzinfo=UTC)          # exactly at the reset: the new day
    # DST: 17:00 New York is 21:00 UTC in summer and 22:00 UTC in winter
    start, _ = pg.last_reset(datetime(2026, 11, 2, 23, 0, tzinfo=UTC), NY, "17:00")
    assert start == datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    start, _ = pg.last_reset(datetime(2026, 10, 30, 23, 0, tzinfo=UTC), NY, "17:00")
    assert start == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    # A reset inside the skipped hour (02:30 on spring-forward day) happens at the first valid time after it
    start, _ = pg.last_reset(datetime(2026, 3, 8, 12, 0, tzinfo=UTC), NY, "02:30")
    assert start.astimezone(NY).hour == 3


# ───────── Daily P&L ─────────
def test_daily_pnl_is_the_realized_sum_and_zero_when_verified_with_no_trades():
    out = pg.build(facts(), "America/New_York")
    d = out["daily_pnl"]
    assert d["value"] == "0.00" and d["status"] == "verified" and d["basis"] == "realized_net" and d["date"] == "2026-11-17"
    f = facts(floating=Decimal("-55.5"))
    f.day.update(net=Decimal("-312.40"), entries=3, positions=2)           # a partial close: 3 entries of 2 positions
    d = pg.build(f, "America/New_York")["daily_pnl"]
    assert d["value"] == "-312.40" and d["closed_entries"] == 3 and d["positions"] == 2 and d["open_pnl"] == "-55.50"


def test_daily_pnl_status_when_history_is_not_verified():
    def status(**kw):
        return pg.build(facts(**kw), "America/New_York")["daily_pnl"]
    _, d0, d1 = pg.local_day(NOW, NY)
    assert status(closed=cov(queued=[(ms(d0) - 10, ms(d0) + 10)]))["status"] == "importing"
    assert status(closed=cov(gaps=[(ms(d0) + 5, ms(d0) + 10)]))["status"] == "gaps"
    assert status(closed=cov(stale=True))["status"] == "delayed"
    assert status(closed=cov(earliest_ms=ms(d0) + 1000))["status"] == "not_imported"
    p = status(closed=cov(synced_at=None))
    assert p["status"] == "pending" and p["value"] is None
    n = status(closed=cov(permitted=False))
    assert n["status"] == "not_permitted" and n["value"] is None
    # a gap on another day does not touch today
    assert status(closed=cov(gaps=[(ms(d0) - 100_000, ms(d0) - 50_000)]))["status"] == "verified"


def test_daily_pnl_works_without_account_setup():
    out = pg.build(facts(setup_enabled=False, profile=None), "UTC")
    assert out["setup"] == "disabled" and out["rows"] == [] and out["overview"] is None and out["daily_pnl"]["status"] == "verified"


# ───────── Evaluation ─────────
def test_evaluation_phase1_percent_target_from_starting_size():
    out = pg.build(facts(evaluation()), "UTC")
    r = row(out, "phase_target")
    # target = 10% x 100,000 = 10,000; progress = 104,200 - 100,000 = 4,200 -> 42%
    assert (r["target"], r["target_percent"], r["current"], r["remaining"], r["ratio_percent"]) == ("10000.00", "10.0", "4200.00", "5800.00", "42.0")
    assert r["baseline"] == {"kind": "starting_size", "amount": "100000.00"} and r["status_label"] == "In progress"
    assert out["overview"]["label"] == "Phase 1 profit target" and out["overview"]["ratio_percent"] == "42.0"


def test_evaluation_amount_target_reached_and_exceeded_is_clamped_but_exact():
    out = pg.build(facts(evaluation(target=money(8000)), balance=Decimal("109000")), "UTC")
    r = row(out, "phase_target")
    assert r["status_label"] == "Target reached" and r["fraction"] == 1.0 and r["ratio_percent"] == "112.5" and r["over_by"] == "1000.00" and r["remaining"] == "0.00"
    assert r["target_percent"] == "8.0"


def test_later_phase_needs_its_own_confirmed_starting_balance():
    out = pg.build(facts(evaluation(current=2)), "UTC")
    r = row(out, "phase_target")
    assert not r["measurable"] and r["action"]["flow"] == "phaseStart" and "Phase 2" in r["reason"]
    assert out["overview"]["state"] == "unavailable" and out["overview"]["action"]["flow"] == "phaseStart"
    ok = row(pg.build(facts(evaluation(current=2, phaseStart={"phase": 2, "amount": "100000"})), "UTC"), "phase_target")
    assert ok["measurable"] and ok["baseline"]["kind"] == "confirmed_phase_start"
    stale = row(pg.build(facts(evaluation(current=2, phaseStart={"phase": 1, "amount": "100000"})), "UTC"), "phase_target")
    assert not stale["measurable"]                         # confirmed for Phase 1, not for the phase now in progress


def test_unknown_phase_and_missing_size_offer_the_matching_action():
    r = row(pg.build(facts(evaluation(current=None)), "UTC"), "phase_target")
    assert not r["measurable"] and r["action"]["step"] == "phaseCount"
    p = evaluation()
    del p["size"]
    r = row(pg.build(facts(p), "UTC"), "phase_target")
    assert not r["measurable"] and r["action"]["step"] == "size"


def test_cash_after_funding_never_counts_as_progress():
    f = facts(evaluation(target=money(10000)), balance=Decimal("105000"))
    f.cash.append({"id": "L1", "type": "DEPOSIT", "amount": Decimal("2000"), "at": NOW - timedelta(days=3)})
    r = row(pg.build(f, "UTC"), "phase_target")
    assert r["current"] == "3000.00" and r["cash_excluded"] == "2000.00"              # 105,000 - 100,000 - 2,000 deposited
    f = facts(evaluation(target=money(10000)), balance=Decimal("103000"))
    f.cash.append({"id": "W1", "type": "WITHDRAWAL", "amount": Decimal("-1500"), "at": NOW - timedelta(days=3)})
    assert row(pg.build(f, "UTC"), "phase_target")["current"] == "4500.00"           # the 1,500 withdrawn was not a trading loss
    r = row(pg.build(facts(evaluation(target=money(10000)), balance=Decimal("98500")), "UTC"), "phase_target")
    assert r["current"] == "-1500.00" and r["status_label"] == "Below starting point" and r["fraction"] == 0.0


def test_the_funding_deposit_is_the_opening_balance_not_a_later_deposit():
    f = facts(evaluation(target=money(10000)), balance=Decimal("104000"))
    f.cash = [{"id": "L0", "type": "DEPOSIT", "amount": Decimal("100000"), "at": CREATED + timedelta(days=5)}]   # too late to be the funding
    r = row(pg.build(f, "UTC"), "phase_target")
    assert r["cash_excluded"] == "100000.00"


def test_targets_are_unconfirmed_without_the_cash_history_behind_them():
    f = facts(evaluation(target=money(4000)), ledgers=cov(earliest_ms=ms(NOW - timedelta(days=7))), balance=Decimal("104500"))
    r = row(pg.build(f, "UTC"), "phase_target")
    assert r["measurable"] and r["status_label"] == "Unconfirmed" and r["action"]["id"] == "import" and r["current"] == "4500.00"
    out = pg.build(f, "UTC")
    assert out["overview"]["state"] == "unconfirmed"                                # never "Target reached" on incomplete cash history
    f = facts(evaluation(target=money(4000)), balance=Decimal("104500"))
    f.cash.append({"id": "X", "type": "ADJUSTMENT", "amount": Decimal("12"), "at": NOW - timedelta(days=1)})
    assert row(pg.build(f, "UTC"), "phase_target")["status_label"] == "Unconfirmed"
    f = facts(evaluation(current=2, phaseStart={"phase": 2, "amount": "100000"}, target=money(4000)), balance=Decimal("104500"))
    f.cash.append({"id": "D2", "type": "DEPOSIT", "amount": Decimal("500"), "at": NOW - timedelta(days=4)})
    assert row(pg.build(f, "UTC"), "phase_target")["status_label"] == "Unconfirmed"  # cash during an evaluation with an unknown phase start date


# ───────── Master ─────────
def test_master_shows_the_traders_own_payout_target_first():
    out = pg.build(facts(master(goal={"id": "payout"}, payoutTarget=money(6000), firmMin=money(1, "percent"))), "UTC")
    assert [r["id"] for r in out["rows"]] == ["payout_target", "firm_min"]
    assert out["overview"]["label"] == "Your payout target" and out["overview"]["ratio_percent"] == "70.0"   # 4,200 / 6,000
    fm = row(out, "firm_min")
    assert fm["target"] == "1000.00" and fm["status_label"] == "Target reached" and fm["eligibility_note"]


def test_master_falls_back_to_the_firm_minimum_and_says_so():
    out = pg.build(facts(master(goal={"id": "payout"}, firmMin=money(5000))), "UTC")
    assert out["overview"]["label"] == "Firm minimum for payout" and out["overview"]["ratio_percent"] == "84.0"


def test_same_target_entered_twice_is_one_row_that_keeps_both_meanings():
    out = pg.build(facts(master(goal={"id": "payout"}, payoutTarget=money(5, "percent"), firmMin=money(5000))), "UTC")
    assert [r["id"] for r in out["rows"]] == ["payout_target"]
    assert row(out, "payout_target")["also"][0]["id"] == "firm_min"


def test_growth_goal_without_a_number_offers_to_set_it():
    out = pg.build(facts(master(goal={"id": "grow"})), "UTC")
    assert out["rows"] == [] and out["overview"]["state"] == "no_target" and out["overview"]["action"]["flow"] == "growth"


def test_profit_split_keeps_the_traders_share_separate_from_gross_profit():
    r = row(pg.build(facts(master(goal={"id": "payout"}, payoutTarget=money(6000), profitSplit="80")), "UTC"), "payout_target")
    assert (r["current"], r["your_share"], r["target"], r["target_share"], r["basis"]) == ("4200.00", "3360.00", "6000.00", "4800.00", "gross_profit")


def test_a_withdrawal_needs_the_cycle_start_and_later_cash_is_taken_out():
    f = facts(master(goal={"id": "payout"}, payoutTarget=money(4000)), balance=Decimal("101500"))
    w_at = NOW - timedelta(days=20)
    f.cash.append({"id": "W1", "type": "WITHDRAWAL", "amount": Decimal("-3200"), "at": w_at})
    r = row(pg.build(f, "UTC"), "payout_target")
    assert not r["measurable"] and r["action"]["flow"] == "cycle" and r["action"]["withdrawal"] == "3200.00"
    f.profile["master"]["cycleStart"] = {"amount": "100000", "after": pg.iso(w_at)}
    f.cash.append({"id": "D1", "type": "DEPOSIT", "amount": Decimal("500"), "at": NOW - timedelta(days=5)})
    r = row(pg.build(f, "UTC"), "payout_target")
    assert r["measurable"] and r["current"] == "1000.00" and r["baseline"]["kind"] == "confirmed_cycle_start"   # 101,500 - 100,000 - 500
    f.cash.append({"id": "W2", "type": "WITHDRAWAL", "amount": Decimal("-500"), "at": NOW - timedelta(days=1)})
    assert not row(pg.build(f, "UTC"), "payout_target")["measurable"]       # a newer withdrawal: confirm again


def test_master_payout_is_unconfirmed_until_withdrawals_are_imported():
    f = facts(master(goal={"id": "payout"}, payoutTarget=money(6000)), ledgers=cov(earliest_ms=ms(NOW - timedelta(days=7))))
    r = row(pg.build(f, "UTC"), "payout_target")
    assert r["measurable"] and r["status_label"] == "Unconfirmed" and "imported" in r["unconfirmed_reason"]


# ───────── firm daily loss ─────────
def daily_profile(**fr):
    p = evaluation()
    p["firmRules"] = {"dailyLoss": money(5, "percent", "sod_balance"), "dailyReset": "17:00", "dailyResetZone": "America/New_York", **fr}
    return p


def firm_day(f, net="0", unvalued=0):
    start, _ = pg.last_reset(NOW, NY, "17:00")
    f.firm_day = {"net": Decimal(net), "entries": 4, "positions": 3, "unvalued": unvalued, "start": start, "cut": f.summary_at}
    return f


def test_daily_loss_counts_realized_and_open_results_since_the_reset():
    f = firm_day(facts(daily_profile(), balance=Decimal("104200"), equity=Decimal("103400"), floating=Decimal("-800")), "-1300")
    r = row(pg.build(f, "UTC"), "daily_loss")
    # used = -(-1,300 - 800) = 2,100; start-of-day balance = 104,200 + 1,300 = 105,500; limit 5% = 5,275
    assert r["reference"] == {"kind": "sod_balance", "amount": "105500.00"}
    assert (r["limit"], r["used"], r["remaining"], r["floor"], r["ratio_percent"]) == ("5275.00", "2100.00", "3175.00", "100225.00", "39.8")
    assert r["status_label"] == "Within limit" and r["open_pnl"] == "-800.00" and r["definition"] == "daily_loss"


def test_cash_during_the_day_is_neither_a_gain_nor_a_loss():
    start, _ = pg.last_reset(NOW, NY, "17:00")
    for kind, amount in (("WITHDRAWAL", "-1000"), ("DEPOSIT", "1000")):
        bal = Decimal("104200") + Decimal(amount)
        f = firm_day(facts(daily_profile(), balance=bal, equity=bal - 800, floating=Decimal("-800")), "-1300")
        f.cash.append({"id": "C", "type": kind, "amount": Decimal(amount), "at": start + timedelta(hours=2)})
        r = row(pg.build(f, "UTC"), "daily_loss")
        assert r["used"] == "2100.00" and r["reference"]["amount"] == "105500.00" and r["cash_since_reset"] == pg.money(Decimal(amount))


def test_daily_loss_near_and_breached():
    # limit 5,000 (amount); nothing closed since the reset
    for floating, used, label, tone in (("-3700", "3700.00", "Close to limit", "amber"), ("-5000", "5000.00", "Limit reached", "red"), ("-5200", "5200.00", "Limit breached", "red")):
        f = firm_day(facts(daily_profile(dailyLoss=money(5000, "amount", "sod_balance")), floating=Decimal(floating)))
        r = row(pg.build(f, "UTC"), "daily_loss")
        assert (r["used"], r["status_label"], r["tone"]) == (used, label, tone)
    assert r["fraction"] == 1.0 and r["ratio_percent"] == "104.0" and r["remaining"] == "-200.00"


def test_a_starting_size_limit_does_not_need_cash_history_but_a_start_of_day_percent_does():
    p = daily_profile(dailyLoss=money(4, "percent", "starting"), dailyFrom="sod_balance")
    f = firm_day(facts(p, floating=Decimal("-500"), ledgers=cov(earliest_ms=ms(NOW))), "-300")
    r = row(pg.build(f, "UTC"), "daily_loss")
    assert r["measurable"] and r["limit"] == "4000.00" and r["used"] == "800.00" and r["reference"] is None
    f = firm_day(facts(daily_profile(), floating=Decimal("-500"), ledgers=cov(earliest_ms=ms(NOW))), "-300")
    assert not row(pg.build(f, "UTC"), "daily_loss")["measurable"]


def test_daily_loss_needs_reset_time_zone_and_what_it_counts_from():
    p = daily_profile()
    del p["firmRules"]["dailyResetZone"]
    assert row(pg.build(facts(p), "UTC"), "daily_loss")["action"]["step"] == "firmDaily"
    r = row(pg.build(facts(daily_profile(dailyLoss=money(5, "percent", "starting"))), "UTC"), "daily_loss")
    assert not r["measurable"] and "counted from" in r["reason"]
    r = row(pg.build(facts(daily_profile(dailyLoss=money(5, "percent", "sod_equity"))), "UTC"), "daily_loss")
    assert not r["measurable"] and "start-of-day equity" in r["reason"]


def test_daily_loss_waits_for_trade_history_covering_the_day():
    start, _ = pg.last_reset(NOW, NY, "17:00")
    f = firm_day(facts(daily_profile(), closed=cov(queued=[(ms(start) - 5, ms(start) + 5)])))
    assert row(pg.build(f, "UTC"), "daily_loss")["status_label"] == "Updating"
    f = firm_day(facts(daily_profile(), closed=cov(synced_at=NOW - timedelta(minutes=10))))
    assert row(pg.build(f, "UTC"), "daily_loss")["status_label"] == "Updating"       # trades syncing far behind the balance
    f = firm_day(facts(daily_profile()), unvalued=1)
    assert not row(pg.build(f, "UTC"), "daily_loss")["measurable"]


def test_daily_loss_day_follows_the_reset_zone_across_daylight_saving():
    # 17:00 New York is 21:00 UTC in summer and 22:00 UTC in winter; the day starts there either way
    for now, expect in ((datetime(2026, 7, 1, 23, 0, tzinfo=UTC), datetime(2026, 7, 1, 21, 0, tzinfo=UTC)),
                        (datetime(2026, 12, 1, 23, 0, tzinfo=UTC), datetime(2026, 12, 1, 22, 0, tzinfo=UTC))):
        assert pg.last_reset(now, NY, "17:00")[0] == expect


# ───────── overall drawdown ─────────
def overall(mode, ref, value=money(10, "percent", "starting")):
    p = master(goal={"id": "limits"})
    p["firmRules"] = {"overall": value, "overallMode": mode, **({"overallRef": ref} if ref else {})}
    return p


def test_static_drawdown_on_equity_and_balance_with_cash_taken_out():
    f = facts(overall("static", "equity"), balance=Decimal("96000"), equity=Decimal("94500"))
    r = row(pg.build(f, "UTC"), "overall_drawdown")
    assert (r["limit"], r["floor"], r["used"], r["remaining"]) == ("10000.00", "90000.00", "5500.00", "4500.00")
    assert r["definition"] == "static_drawdown"
    f = facts(overall("static", "balance"), balance=Decimal("94000"), equity=Decimal("94000"))
    f.cash.append({"id": "W", "type": "WITHDRAWAL", "amount": Decimal("-2000"), "at": NOW - timedelta(days=2)})
    r = row(pg.build(f, "UTC"), "overall_drawdown")
    assert r["used"] == "4000.00" and r["cash_excluded"] == "-2000.00"               # the 2,000 payout is not drawdown
    r = row(pg.build(facts(overall("static", None)), "UTC"), "overall_drawdown")
    assert not r["measurable"] and r["action"]["step"] == "firmOverall"


def test_trailing_balance_peak_is_rebuilt_from_trades_in_time_order():
    f = facts(overall("trailing", "balance", money(5, "percent", "high_water")), balance=Decimal("104200"), equity=Decimal("104000"))
    f.peak = {"total": Decimal("4200"), "nulls": 0, "events": 40, "max_cum": Decimal("8000"), "peak_at": NOW - timedelta(days=5)}
    r = row(pg.build(f, "UTC"), "overall_drawdown")
    # funding 100,000; trades peaked at +8,000 -> 108,000; limit 5% = 5,400; floor 102,600; used 3,800
    assert (r["reference"]["amount"], r["limit"], r["floor"], r["used"], r["remaining"]) == ("108000.00", "5400.00", "102600.00", "3800.00", "1600.00")
    assert r["status_label"] == "Close to limit" and r["definition"] == "trailing_drawdown"


def test_trailing_balance_ignores_payouts_when_finding_the_peak():
    f = facts(overall("trailing", "balance", money(5000, "amount", "high_water")), balance=Decimal("101200"), equity=Decimal("101200"))
    f.cash.append({"id": "W", "type": "WITHDRAWAL", "amount": Decimal("-3000"), "at": NOW - timedelta(days=10)})
    f.peak = {"total": Decimal("4200"), "nulls": 0, "events": 40, "max_cum": Decimal("4200"), "peak_at": NOW - timedelta(days=1)}
    r = row(pg.build(f, "UTC"), "overall_drawdown")
    assert r["used"] == "0.00" and r["reference"]["amount"] == "104200.00"          # the payout is not a 3,000 drawdown


def test_trailing_balance_needs_coverage_even_when_totals_add_up():
    f = facts(overall("trailing", "balance", money(5, "percent", "high_water")), balance=Decimal("104200"))
    f.peak = {"total": Decimal("4200"), "nulls": 0, "events": 40, "max_cum": Decimal("8000"), "peak_at": NOW}
    f.closed = cov(earliest_ms=ms(NOW - timedelta(days=7)))           # the arithmetic still adds up, the history doesn't reach the opening
    r = row(pg.build(f, "UTC"), "overall_drawdown")
    assert not r["measurable"] and r["action"]["id"] == "import"
    f = facts(overall("trailing", "balance", money(5, "percent", "high_water")), balance=Decimal("104200"))
    f.peak = {"total": Decimal("3000"), "nulls": 0, "events": 40, "max_cum": Decimal("8000"), "peak_at": NOW}   # 1,200 unexplained
    assert "doesn't add back" in row(pg.build(f, "UTC"), "overall_drawdown")["reason"]
    f.peak = {"total": Decimal("4200"), "nulls": 1, "events": 40, "max_cum": Decimal("8000"), "peak_at": NOW}
    assert not row(pg.build(f, "UTC"), "overall_drawdown")["measurable"]


def test_trailing_equity_is_not_measured():
    r = row(pg.build(facts(overall("trailing", "equity", money(5, "percent", "high_water_equity"))), "UTC"), "overall_drawdown")
    assert not r["measurable"] and "highest equity" in r["reason"]


# ───────── personal limits ─────────
def personal(**pr):
    p = master(goal={"id": "limits"})
    p["personal"] = pr
    return p


def pos(symbol, side, o, c, sl, profit):
    return {"symbol": symbol, "alias": None, "side": side, "volume": "1", "open_price": o, "current_price": c, "sl_price": sl, "profit": profit}


def risk(positions, limit=money(500), quotes=None, **kw):
    return row(pg.build(facts(personal(riskPerTrade=limit), positions=positions, quotes=quotes or {}, **kw), "UTC"), "risk_per_trade")


def test_risk_is_definite_only_when_the_instrument_is_priced_in_the_account_currency():
    long_ = pos("XAUUSD", "BUY", "2400", "2404", "2392", "200")       # 50 per unit; stop 8 below: 400
    short = pos("US30", "SELL", "40000", "39960", "40030", "40")      # 1 per unit; stop 30 above: 30
    r = risk([long_, short], quotes={"XAUUSD": "USD", "US30": "USD"})
    assert (r["used"], r["worst_symbol"], r["status_label"], r["tone"]) == ("400.00", "XAUUSD", "Close to limit", "amber")   # 80 % of 500
    r = risk([long_, short], limit=money(1000), quotes={"XAUUSD": "USD", "US30": "USD"})
    assert r["status_label"] == "Within limit" and r["tone"] == "green"
    r = risk([long_, short])                                          # quote currency unknown: estimate only
    assert r["used"] == "400.00" and r["status_label"] == "Estimate only" and r["tone"] == "neutral" and r["estimate"]
    r = risk([pos("USDJPY", "BUY", "150", "150.5", "149", "333.33")], limit=money(1000), quotes={"USDJPY": "JPY"})
    assert r["status_label"] == "Estimate only"                       # converted at today's rate
    r = risk([pos("XAUUSD", "BUY", "2400", "2404", "2385", "200")])   # estimate 750 over a 500 limit
    assert r["status_label"] == "Estimate over limit" and r["tone"] == "amber"
    r = risk([pos("XAUUSD", "BUY", "2400", "2404", "2390", "200")], quotes={"XAUUSD": "USD"})
    assert r["status_label"] == "Limit reached" and r["tone"] == "red"


def test_risk_unavailable_cases_say_why():
    r = risk([pos("XAUUSD", "BUY", "2400", "2396", None, "-200")])
    assert not r["measurable"] and r["status_label"] == "Risk unavailable" and "no stop loss" in r["reason"]
    r = risk([pos("XAUUSD", "BUY", "2400", "2400", "2390", "0")])
    assert "too close to entry" in r["reason"]
    r = risk([pos("XAUUSD", "BUY", "2400", "2400.2", "2390", "1.00")])   # 0.008 % move, 1.00 P&L
    assert "too close to entry" in r["reason"]
    r = risk([pos("XAUUSD", "BUY", "2400", "2404", "2390", None)])
    assert "no P&L" in r["reason"]
    r = risk([pos("XAUUSD", "BUY", "2400", "2404", "2390", "-200")])     # P&L disagrees with the price move
    assert not r["measurable"]


def test_a_stop_beyond_breakeven_has_no_risk_and_partial_coverage_is_not_within_limit():
    r = risk([pos("XAUUSD", "BUY", "2400", "2410", "2405", "500"), pos("US30", "SELL", "40000", "39900", "39950", "100")])
    assert r["used"] == "0.00" and all(p["stop_in_profit"] for p in r["positions"]) and r["status_label"] == "Within limit"
    r = risk([pos("XAUUSD", "BUY", "2400", "2404", "2392", "200"), pos("US30", "SELL", "40000", "39960", None, "40")], quotes={"XAUUSD": "USD"})
    assert r["status_label"] == "Partly measured" and r["unmeasured_count"] == 1


def test_risk_uses_gross_profit_so_fees_do_not_skew_the_value_per_unit():
    p = pos("XAUUSD", "BUY", "2400", "2404", "2392", "200")
    p["net_profit"] = "190"                                           # ignored: commission is not a price move
    assert risk([p], quotes={"XAUUSD": "USD"})["used"] == "400.00"


def test_no_open_trades_and_session_budget():
    none = risk([])
    assert not none["measurable"] and none["status_label"] == "No open trades"
    r = row(pg.build(facts(personal(sessionBudget=money(500))), "UTC"), "session_budget")
    assert not r["measurable"] and r["action"]["flow"] == "personal"


def test_no_optional_settings_means_no_rows():
    out = pg.build(facts(master(goal={"id": "limits"})), "UTC")
    assert out["rows"] == [] and out["labels"] == []
    out = pg.build(facts({"status": "new"}), "UTC")
    assert out["rows"] == [] and out["overview"]["state"] == "setup"
    assert pg.build(facts(personal(style="Swing")), "UTC")["labels"] == [{"title": "Trading style", "value": "Swing"}]
