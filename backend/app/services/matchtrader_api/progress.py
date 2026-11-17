"""Overview progress for one Match-Trader connection.

What the dashboard shows above Recent trades and in the Daily P&L / Challenge-payout tiles:

* ``daily_pnl``  - realized net result of the entries closed during the current calendar day in
  the dashboard's time zone;
* ``overview``   - the one target the Challenge / payout tile shows;
* ``rows``       - one progress row per target, firm rule and personal limit the trader entered;
* ``labels``     - categorical answers (trading style) shown as short labels.

Every figure is computed here, on the server, from the stored broker data and the saved setup
profile, in one repeatable-read transaction and a fixed number of queries (never one per trade
or per row). Nothing is inferred: a threshold, starting balance, reset time, cycle start or
reference the trader did not give - or that the broker does not report - makes that row
"unavailable" with the reason and the setup action that would supply it. ``DEFINITIONS`` below is
returned with the figures and is the exact contract the tests check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")          # reconstruction check: history must add up to the cent
NEAR_LIMIT = Decimal("0.70")         # a limit 70 % used or more reads "Close to limit"
MAX_CASH_ROWS = 200
TRADE_TYPES = {"CLOSE_POSITION"}     # ledger rows that mirror a closed trade (already in closed positions)

DEFINITIONS = {
    "daily_pnl": "Sum of the broker's net result (commission and swap already included, never subtracted again) of every closing entry whose close time falls on the current calendar day in the dashboard time zone. A position closed in parts counts each part once; duplicates cannot occur because entries are stored once per broker uid. Open positions are not included.",
    "challenge_progress": "Evaluation: current broker balance minus the phase starting balance, minus any deposit after the account was funded. The starting size counts as the phase start only for a one-phase evaluation or Phase 1; later phases need the starting balance the trader confirmed for that phase. Open trades are not counted.",
    "payout_progress": "Master: current broker balance minus the starting size (or the cycle start the trader confirmed after the latest withdrawal), minus deposits after that point. This is profit before the firm's profit split, not the amount that can be withdrawn.",
    "daily_loss": "Start-of-day reference minus current equity (so open losses count). The day starts at the trader's reset time in the reset time zone. Start-of-day balance = current balance minus net results closed since the reset minus deposits/withdrawals since the reset; it needs imported history covering the whole day. Start-of-day equity is not recorded, so a rule measured from it is not measured.",
    "static_drawdown": "Starting size minus current balance or equity (as the rule says). Floor = starting size minus the limit.",
    "trailing_drawdown": "Highest balance minus current balance. The highest balance is rebuilt from the full imported trade and cash history and is used only when that history adds back exactly to the account's start. Equity peaks cannot be rebuilt, so equity-trailing rules are not measured.",
    "risk_per_trade": "For each open position with a stop loss: the loss at the stop, valued with the broker's current P&L on that position (P&L per price unit = current profit / (current price - open price)). Approximate when the quote currency differs from the account currency.",
    "session_budget": "Not measured: TaliTrade does not know when a trading session starts and ends.",
}


# ───────────────────────────── small helpers ─────────────────────────────
def D(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(CENT, rounding=ROUND_HALF_EVEN), "f")


def pct(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN), "f")


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def zone(name: str | None) -> ZoneInfo | None:
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_day(now: datetime, tz: ZoneInfo) -> tuple[date, datetime, datetime]:
    """The calendar day containing ``now`` in ``tz`` and its [start, end) in UTC. Uses wall-clock
    midnights, so a 23- or 25-hour day around a daylight-saving change has its true length."""
    day = now.astimezone(tz).date()
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    return day, start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def last_reset(now: datetime, tz: ZoneInfo, hhmm: str) -> tuple[datetime, datetime]:
    """Most recent wall-clock ``hhmm`` in ``tz`` at or before ``now`` and the next one (UTC)."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(tz)

    def at(d: date) -> datetime:
        # fold=0: a time that happens twice (fall back) means its first occurrence; a time skipped
        # by spring forward falls the length of the gap later (02:30 -> 03:30).
        return datetime.combine(d, time(h, m), tzinfo=tz).astimezone(timezone.utc)

    today = at(local.date())
    if today > now:
        return at(local.date() - timedelta(days=1)), today
    return today, at(local.date() + timedelta(days=1))


def ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


# ───────────────────────────── facts (SQL) ─────────────────────────────
@dataclass
class Coverage:
    """What the stored history can vouch for, per resource."""
    permitted: bool = True
    synced_at: datetime | None = None          # last successful sync of the resource
    stale: bool = False
    earliest_ms: int | None = None             # earliest requested history (any interval)
    gaps: list[tuple[int, int]] = field(default_factory=list)
    queued: list[tuple[int, int]] = field(default_factory=list)   # requested import still running

    def covers(self, start: datetime, end: datetime) -> str:
        """``verified`` | ``importing`` | ``gaps`` | ``not_imported`` | ``pending`` | ``not_permitted``."""
        if not self.permitted:
            return "not_permitted"
        if self.synced_at is None:
            return "pending"
        a, b = ms(start), ms(end)
        if any(f < b and t > a for f, t in self.queued):
            return "importing"
        if any(f < b and t > a for f, t in self.gaps):
            return "gaps"
        if self.earliest_ms is None or self.earliest_ms > a:
            return "not_imported"
        return "verified"


@dataclass
class Facts:
    now: datetime
    currency: str | None
    account_created_at: datetime | None
    balance: Decimal | None = None
    equity: Decimal | None = None
    floating: Decimal | None = None
    summary_at: datetime | None = None
    summary_stale: bool = False
    positions: list[dict] | None = None
    closed: Coverage = field(default_factory=Coverage)
    ledgers: Coverage = field(default_factory=Coverage)
    day: dict = field(default_factory=dict)          # aggregates for the dashboard day
    firm_day: dict | None = None                     # aggregates since the firm reset (to summary time)
    all_trades: dict = field(default_factory=dict)
    cash: list[dict] = field(default_factory=list)   # non-trade ledger rows, oldest first
    cash_truncated: bool = False
    peak: dict | None = None                         # balance reconstruction, when asked for
    profile: dict | None = None
    setup_enabled: bool = False


def _trade_agg(row) -> dict:
    return {"net": D(row["net"]) or Decimal(0), "entries": int(row["entries"] or 0), "positions": int(row["positions"] or 0),
            "unvalued": int(row["unvalued"] or 0), "last": row.get("last")}


async def gather(conn, read_model, connection_id: int, user_id: str, *, tz: ZoneInfo, setup_enabled: bool) -> Facts | None:
    """Every stored fact the rows need. Returns None when the connection is not owned by the user."""
    params: dict[str, Any] = {"id": connection_id, "user": user_id}
    c = (await conn.execute(text("""
        SELECT c.id, c.account_currency, c.account_created_at, c.capabilities, c.platform_host, c.external_login
        FROM mtapi_connections c WHERE c.id = :id AND c.user_id = :user AND c.status <> 'disconnected'"""), params)).mappings().first()
    if not c:
        return None
    from app.services.matchtrader_api.read_models import _freshness
    now = (await conn.execute(text("SELECT NOW()"))).scalar()
    fresh = _freshness(await read_model._states(conn, connection_id, user_id), c["capabilities"] or {}, now)
    f = Facts(now=now, currency=c["account_currency"], account_created_at=c["account_created_at"], setup_enabled=setup_enabled)

    if fresh["summary"]["last_success_at"]:
        s = (await conn.execute(text("SELECT balance, equity, profit, fetched_at FROM mtapi_account_summary WHERE connection_id = :id"), params)).mappings().first()
        if s:
            f.balance, f.equity, f.floating, f.summary_at = D(s["balance"]), D(s["equity"]), D(s["profit"]), s["fetched_at"]
            f.summary_stale = bool(fresh["summary"].get("is_stale"))
    if fresh["positions"]["last_success_at"]:
        rows = (await conn.execute(text("""SELECT external_id, symbol, alias, side, volume, open_price, current_price, sl_price, profit
                                            FROM mtapi_positions WHERE connection_id = :id ORDER BY open_time NULLS LAST, external_id"""), params)).mappings().all()
        f.positions = [dict(r) for r in rows]

    for name, cov in (("closed_positions", f.closed), ("ledgers", f.ledgers)):
        st = fresh[name]
        cov.permitted = st.get("status") != "not_permitted"
        cov.synced_at = _parse_dt(st.get("last_success_at"))
        cov.stale = bool(st.get("is_stale"))
    ivs = (await conn.execute(text("""
        SELECT resource, status, kind, range_from, range_to FROM mtapi_history_intervals
        WHERE connection_id = :id AND resource IN ('closed_positions', 'ledgers')
          AND (status = 'gap' OR (status = 'queued' AND kind = 'backfill'))
        UNION ALL
        SELECT resource, 'earliest', NULL, MIN(range_from), NULL FROM mtapi_history_intervals
        WHERE connection_id = :id AND resource IN ('closed_positions', 'ledgers') AND status <> 'cancelled' GROUP BY resource
        LIMIT 1000"""), params)).mappings().all()
    for r in ivs:
        cov = f.closed if r["resource"] == "closed_positions" else f.ledgers
        if r["status"] == "earliest":
            cov.earliest_ms = int(r["range_from"]) if r["range_from"] is not None else None
        elif r["status"] == "gap":
            cov.gaps.append((int(r["range_from"]), int(r["range_to"])))
        else:
            cov.queued.append((int(r["range_from"]), int(r["range_to"])))

    profile = None
    if setup_enabled:
        row = (await conn.execute(text("""SELECT data FROM account_setup_profiles WHERE user_id = :user AND provider = 'matchtrader_api'
                                           AND platform_host = :h AND external_login = :l"""),
                                  {**params, "h": c["platform_host"], "l": c["external_login"]})).mappings().first()
        profile = dict(row["data"]) if row and row["data"] else None
    f.profile = profile

    _, d0, d1 = local_day(now, tz)
    fr = (profile or {}).get("firmRules") or {}
    f0 = None
    rz = zone(fr.get("dailyResetZone"))
    if fr.get("dailyLoss") and fr.get("dailyReset") and rz:
        f0, _ = last_reset(now, rz, fr["dailyReset"])
    cut = f.summary_at or now
    agg = (await conn.execute(text("""
        SELECT COUNT(*) FILTER (WHERE close_time >= :d0 AND close_time < :d1) AS d_entries,
               COUNT(DISTINCT position_id) FILTER (WHERE close_time >= :d0 AND close_time < :d1) AS d_positions,
               COALESCE(SUM(net_profit) FILTER (WHERE close_time >= :d0 AND close_time < :d1), 0) AS d_net,
               COUNT(*) FILTER (WHERE close_time >= :d0 AND close_time < :d1 AND net_profit IS NULL) AS d_unvalued,
               MAX(close_time) FILTER (WHERE close_time >= :d0 AND close_time < :d1) AS d_last,
               COUNT(*) FILTER (WHERE close_time >= :f0 AND close_time <= :cut) AS f_entries,
               COUNT(DISTINCT position_id) FILTER (WHERE close_time >= :f0 AND close_time <= :cut) AS f_positions,
               COALESCE(SUM(net_profit) FILTER (WHERE close_time >= :f0 AND close_time <= :cut), 0) AS f_net,
               COUNT(*) FILTER (WHERE close_time >= :f0 AND close_time <= :cut AND net_profit IS NULL) AS f_unvalued,
               COUNT(*) AS a_entries, COUNT(DISTINCT position_id) AS a_positions, COALESCE(SUM(net_profit), 0) AS a_net,
               COUNT(*) FILTER (WHERE net_profit IS NULL) AS a_unvalued, MAX(close_time) AS a_last
        FROM mtapi_closed_positions WHERE connection_id = :id"""),
        {**params, "d0": d0, "d1": d1, "f0": f0 or datetime.max.replace(tzinfo=timezone.utc), "cut": cut})).mappings().first()
    pick = lambda p: _trade_agg({"net": agg[p + "_net"], "entries": agg[p + "_entries"], "positions": agg[p + "_positions"],
                                  "unvalued": agg[p + "_unvalued"], "last": agg.get(p + "_last")})
    f.day = {**pick("d"), "date": local_day(now, tz)[0], "start": d0, "end": d1}
    f.all_trades = pick("a")
    if f0:
        f.firm_day = {**pick("f"), "start": f0, "cut": cut}

    rows = (await conn.execute(text(f"""SELECT external_id, entry_type, amount, entry_time FROM mtapi_ledgers
                                         WHERE connection_id = :id AND (entry_type IS NULL OR UPPER(entry_type) <> 'CLOSE_POSITION')
                                         ORDER BY entry_time, external_id LIMIT {MAX_CASH_ROWS + 1}"""), params)).mappings().all()
    f.cash = [{"id": r["external_id"], "type": (r["entry_type"] or "").upper(), "amount": D(r["amount"]), "at": r["entry_time"]} for r in rows[:MAX_CASH_ROWS]]
    f.cash_truncated = len(rows) > MAX_CASH_ROWS

    if (fr.get("overall") and fr.get("overallMode") == "trailing" and fr.get("overallRef") == "balance") and f.balance is not None:
        p = (await conn.execute(text("""
            WITH ev AS (
                SELECT close_time AS t, net_profit AS a, 0 AS o, uid AS k FROM mtapi_closed_positions WHERE connection_id = :id AND close_time <= :cut
                UNION ALL
                SELECT entry_time, amount, 1, external_id FROM mtapi_ledgers
                WHERE connection_id = :id AND (entry_type IS NULL OR UPPER(entry_type) <> 'CLOSE_POSITION') AND entry_time <= :cut
            ), run AS (SELECT t, SUM(a) OVER (ORDER BY t, o, k ROWS UNBOUNDED PRECEDING) AS cum FROM ev)
            SELECT (SELECT COALESCE(SUM(a), 0) FROM ev) AS total, (SELECT COUNT(*) FROM ev WHERE a IS NULL) AS nulls,
                   (SELECT COUNT(*) FROM ev) AS events, MAX(cum) AS max_cum,
                   (SELECT t FROM run ORDER BY cum DESC, t LIMIT 1) AS peak_at FROM run"""), {**params, "cut": cut})).mappings().first()
        f.peak = {"total": D(p["total"]) or Decimal(0), "nulls": int(p["nulls"] or 0), "events": int(p["events"] or 0),
                  "max_cum": D(p["max_cum"]), "peak_at": p["peak_at"]}
    return f


def _parse_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ───────────────────────────── building the view ─────────────────────────────
def action(label: str, kind: str = "flow", flow: str | None = None, step: str | None = None) -> dict:
    out = {"id": kind, "label": label}
    if flow:
        out["flow"] = flow
    if step:
        out["step"] = step
    return out


ACT_SIZE = lambda: action("Add starting size", flow="basics", step="size")
ACT_IMPORT = lambda: action("Import trade history", kind="import")


def entered(e: dict | None) -> dict | None:
    if not e:
        return None
    return {"value": str(e.get("value")), "unit": e.get("unit"), "base": e.get("base")}


def resolve(e: dict, bases: dict[str, Decimal | None]) -> tuple[Decimal | None, Decimal | None, str | None]:
    """(amount, percent of its base, missing base id). The entry itself is never rewritten."""
    value, base_id = D(e.get("value")), e.get("base")
    base = bases.get(base_id) if base_id else None
    if e.get("unit") == "amount":
        return value, (value / base * 100 if base else None), None
    if base is None:
        return None, value, base_id or "unknown"
    return value * base / 100, value, None


def _first_funding(cash: list[dict]) -> dict | None:
    """The deposit that funded the account: the first ledger row, if it is a deposit."""
    return cash[0] if cash and "DEPOSIT" in cash[0]["type"] and (cash[0]["amount"] or 0) > 0 else None


def _kind(entry: dict) -> str:
    t = entry["type"]
    if "DEPOSIT" in t:
        return "deposit"
    if "WITHDRAW" in t:
        return "withdrawal"
    return "other"


def _status_limit(used: Decimal, limit: Decimal) -> tuple[str, str, str]:
    if used > limit:
        return "breached", "Limit breached", "red"
    if used == limit:
        return "limit_reached", "Limit reached", "red"
    if limit and used / limit >= NEAR_LIMIT:
        return "near", "Close to limit", "amber"
    return "within", "Within limit", "green"


def _fraction(part: Decimal, whole: Decimal) -> tuple[float, str | None]:
    if not whole:
        return 0.0, None
    ratio = part / whole
    return float(min(max(ratio, Decimal(0)), Decimal(1))), pct(ratio * 100)


def unavailable(row: dict, reason: str, act: dict | None = None, status: str = "unavailable", label: str = "Not measured") -> dict:
    row.update({"measurable": False, "status": status, "status_label": label, "tone": "neutral", "reason": reason})
    if act:
        row["action"] = act
    return row


def build(f: Facts, tz_name: str) -> dict:
    out = {"currency": f.currency, "server_time": iso(f.now), "time_zone": tz_name, "definitions": DEFINITIONS,
           "daily_pnl": daily_pnl(f, tz_name), "rows": [], "labels": [], "overview": None, "setup": "disabled"}
    if not f.setup_enabled:
        return out
    p = f.profile or {}
    out["setup"] = "saved" if p.get("status") == "saved" else (p.get("status") or "new")
    rows, labels = rows_for(f, p)
    out["rows"], out["labels"] = rows, labels
    out["overview"] = overview(p, rows)
    return out


# ── Daily P&L ──
def daily_pnl(f: Facts, tz_name: str) -> dict:
    d = f.day
    cov = f.closed.covers(d["start"], min(d["end"], f.now))
    out = {"basis": "realized_net", "date": d["date"].isoformat(), "time_zone": tz_name, "day_start": iso(d["start"]), "day_end": iso(d["end"]),
           "closed_entries": d["entries"], "positions": d["positions"], "entries_without_result": d["unvalued"],
           "checked_through": iso(f.closed.synced_at), "open_pnl": money(f.floating), "coverage": cov}
    if cov in ("not_permitted", "pending"):
        out.update(value=None, status=cov)
        return out
    out["value"] = money(d["net"])
    if cov == "verified" and f.closed.stale:
        out["status"] = "delayed"
    elif cov == "verified":
        out["status"] = "verified" if not d["unvalued"] else "partial"
    else:
        out["status"] = cov            # importing | gaps | not_imported: value shown with this status
    return out


# ── rows ──
def rows_for(f: Facts, p: dict) -> tuple[list[dict], list[dict]]:
    size = D((p.get("size") or {}).get("amount"))
    kind = p.get("type")
    rows: list[dict] = []
    funding = _first_funding(f.cash)
    later_cash = [c for c in f.cash if c is not funding]
    ledger_from = f.ledgers.earliest_ms
    created_ms = ms(f.account_created_at) if f.account_created_at else None
    ledger_complete = f.ledgers.permitted and ledger_from is not None and created_ms is not None and ledger_from <= created_ms \
        and not f.ledgers.gaps and not f.ledgers.queued and not f.cash_truncated

    def cash_note(since: datetime | None) -> tuple[Decimal, list[dict], list[str]]:
        """Deposits after ``since`` (taken out of progress), withdrawals and unknown entries after it."""
        deposits, other, notes = Decimal(0), [], []
        for c in later_cash:
            if since is not None and c["at"] <= since:
                continue
            k = _kind(c)
            if k == "deposit" and (c["amount"] or 0) > 0:
                deposits += c["amount"]
            else:
                other.append(c)
        if deposits:
            notes.append("deposit")
        if not ledger_complete:
            notes.append("ledger_incomplete")
        return deposits, other, notes

    # ── Evaluation profit target ──
    if kind == "evaluation":
        ev = p.get("evaluation") or {}
        phase = 1 if ev.get("phaseCount") == 1 else ev.get("currentPhase")
        title = f"Phase {phase} profit target" if phase and ev.get("phaseCount", 1) != 1 else "Profit target"
        target = ev.get("phaseTarget")
        if target:
            row = {"id": "phase_target", "group": "target", "kind": "target", "title": title, "source": "Firm target", "entered": entered(target)}
            start, start_note = None, None
            ps = ev.get("phaseStart") or {}
            if phase == 1:
                start, start_note = size, "starting_size"
            elif phase and ps.get("phase") == phase and D(ps.get("amount")):
                start, start_note = D(ps["amount"]), "confirmed_phase_start"
            amount, percent, missing = resolve(target, {"starting": size})
            if not ev.get("phaseCount") or (ev.get("phaseCount", 0) > 1 and not phase):
                rows.append(unavailable(row, "Which phase you are in is not saved, so TaliTrade can't tell where this phase started.", action("Choose current phase", flow="phase", step="phaseCount")))
            elif phase == 1 and size is None:
                rows.append(unavailable(row, "Needs the starting size your firm issued. Phase 1 starts there.", ACT_SIZE()))
            elif start is None:
                rows.append(unavailable(row, f"Needs the balance Phase {phase} started from. Your starting size may not apply to this phase.",
                                        action(f"Confirm Phase {phase} starting balance", flow="phaseStart", step="phaseStart")))
            elif amount is None:
                rows.append(unavailable(row, "This target is a percent of your starting size, which is not saved.", ACT_SIZE()))
            elif f.balance is None:
                rows.append(unavailable(row, "Waiting for the first balance from your broker.", None, status="pending", label="Waiting for data"))
            else:
                deposits, other, notes = cash_note(funding["at"] if funding else None)
                rows.append(target_row(row, f.balance - start - deposits, amount, percent, start, start_note, notes, other, f))
    # ── Master: payout target, firm minimum, growth target ──
    if kind == "master":
        msr = p.get("master") or {}
        withdrawals = [c for c in later_cash if _kind(c) == "withdrawal"]
        last_w = withdrawals[-1] if withdrawals else None
        cyc = msr.get("cycleStart") or {}
        start, start_note, since = size, "starting_size", funding["at"] if funding else None
        cycle_needed = False
        if last_w is not None:
            after = _parse_dt(cyc.get("after"))
            if D(cyc.get("amount")) and after is not None and abs((after - last_w["at"]).total_seconds()) < 1:
                start, start_note, since = D(cyc["amount"]), "confirmed_cycle_start", last_w["at"]
            else:
                cycle_needed = True
        defs = [("payoutTarget", "payout_target", "Your payout target", "Your target"),
                ("firmMin", "firm_min", "Firm minimum profit for payout", "Firm rule"),
                ("growthTarget", "growth_target", "Growth target", "Your target")]
        made: list[dict] = []
        for key, rid, title, source in defs:
            e = msr.get(key)
            if not e:
                continue
            row = {"id": rid, "group": "target", "kind": "target", "title": title, "source": source, "entered": entered(e), "eligibility_note": key != "growthTarget"}
            amount, percent, _ = resolve(e, {"starting": size})
            if size is None and (start_note == "starting_size"):
                made.append(unavailable(row, "Needs the starting size your firm issued: profit is measured from it.", ACT_SIZE()))
            elif amount is None:
                made.append(unavailable(row, "This target is a percent of your starting size, which is not saved.", ACT_SIZE()))
            elif cycle_needed:
                made.append(unavailable(row, f"A withdrawal of {money(abs(last_w['amount'] or 0))} on {last_w['at'].date().isoformat()} is in this account's history. TaliTrade can't tell where the current payout cycle started.",
                                        {**action("Confirm cycle start", flow="cycle", step="cycleStart"), "after": iso(last_w["at"]), "withdrawal": money(abs(last_w["amount"] or 0))}))
            elif f.balance is None:
                made.append(unavailable(row, "Waiting for the first balance from your broker.", None, status="pending", label="Waiting for data"))
            else:
                deposits, other, notes = cash_note(since)
                made.append(target_row(row, f.balance - start - deposits, amount, percent, start, start_note, notes, other, f))
        rows.extend(combine(made))

    rows.extend(firm_rows(f, p, size))
    rows.extend(personal_rows(f, p, size))
    labels = []
    pr = p.get("personal") or {}
    if pr.get("style"):
        labels.append({"title": "Trading style", "value": pr["style"]})
    return rows, labels


def target_row(row: dict, progress: Decimal, target: Decimal, target_pct: Decimal | None, start: Decimal, start_note: str,
               notes: list[str], other_cash: list[dict], f: Facts) -> dict:
    frac, ratio = _fraction(progress, target)
    if progress >= target:
        status, label, tone = "reached", "Target reached", "green"
    elif progress < 0:
        status, label, tone = "below_start", "Below starting point", "amber"
    else:
        status, label, tone = "in_progress", "In progress", "cyan"
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone,
                "target": money(target), "target_percent": pct(target_pct), "current": money(progress),
                "current_percent": pct(progress / start * 100) if start else None,
                "remaining": money(max(target - progress, Decimal(0))), "over_by": money(progress - target) if progress > target else None,
                "fraction": frac, "ratio_percent": ratio, "baseline": {"kind": start_note, "amount": money(start)},
                "measured_on": "balance", "open_pnl_excluded": money(f.floating), "notes": list(notes),
                "balance_at": iso(f.summary_at), "stale": f.summary_stale})
    if other_cash:
        row["notes"].append("other_cash")
        row["other_cash"] = [{"type": c["type"] or "UNKNOWN", "amount": money(c["amount"]), "at": iso(c["at"])} for c in other_cash[-3:]]
    return row


def combine(rows: list[dict]) -> list[dict]:
    """Rows measuring the same thing to the same amount become one row that keeps every meaning."""
    out: list[dict] = []
    for r in rows:
        twin = next((o for o in out if o.get("measurable") and r.get("measurable") and o["target"] == r["target"]
                     and o["baseline"] == r["baseline"]), None)
        if twin:
            twin.setdefault("also", []).append({"id": r["id"], "title": r["title"], "source": r["source"], "entered": r["entered"]})
            twin["eligibility_note"] = twin.get("eligibility_note") or r.get("eligibility_note")
        else:
            out.append(r)
    return out


# ── firm rules ──
def firm_rows(f: Facts, p: dict, size: Decimal | None) -> list[dict]:
    fr = p.get("firmRules") or {}
    rows = []
    if fr.get("dailyLoss"):
        rows.append(daily_loss_row(f, fr, size))
    if fr.get("overall"):
        rows.append(overall_row(f, fr, size))
    return rows


ACT_DAILY = lambda label="Finish daily loss rule": action(label, flow="firm", step="firmDaily")
ACT_OVERALL = lambda label="Finish drawdown rule": action(label, flow="firm", step="firmOverall")


def daily_loss_row(f: Facts, fr: dict, size: Decimal | None) -> dict:
    e = fr["dailyLoss"]
    row = {"id": "daily_loss", "group": "firm", "kind": "limit", "title": "Daily loss limit", "source": "Firm rule", "entered": entered(e),
           "reset": fr.get("dailyReset"), "reset_zone": fr.get("dailyResetZone"), "counted_from": None}
    base = e.get("base")
    counted_from = base if base in ("sod_balance", "sod_equity") else fr.get("dailyFrom")
    row["counted_from"] = counted_from
    if not fr.get("dailyReset") or not zone(fr.get("dailyResetZone")):
        return unavailable(row, "Needs the time your firm's trading day resets and its time zone.", ACT_DAILY("Add reset time"))
    if counted_from not in ("sod_balance", "sod_equity"):
        return unavailable(row, "Needs what each day's loss is counted from: start-of-day balance or equity.", ACT_DAILY())
    if counted_from == "sod_equity":
        return unavailable(row, "Counted from start-of-day equity. TaliTrade does not record equity at your reset time yet, so this can't be measured.", ACT_DAILY("Review daily rule"))
    fd = f.firm_day
    if f.balance is None or f.equity is None or fd is None:
        return unavailable(row, "Waiting for the balance and equity from your broker.", None, status="pending", label="Waiting for data")
    row["day_start"] = iso(fd["start"])
    cut = fd["cut"]
    closed = f.closed.covers(fd["start"], cut)
    ledg = f.ledgers.covers(fd["start"], cut)
    if closed != "verified" or ledg != "verified":
        why = closed if closed != "verified" else ledg
        text_ = {"importing": "Trade history since the reset is still importing.", "gaps": "Part of today's history could not be checked.",
                 "not_imported": "Trade history doesn't reach back to the reset yet.", "pending": "Waiting for the first trade sync.",
                 "not_permitted": "This token can't read the trade or cash history needed to rebuild the start-of-day balance."}[why]
        return unavailable(row, text_, ACT_IMPORT() if why == "not_imported" else None, status="pending" if why in ("importing", "pending") else "unavailable",
                           label="Updating" if why in ("importing", "pending") else "Not measured")
    if fd["unvalued"]:
        return unavailable(row, "A trade closed since the reset has no net result from the broker.")
    cash_today = [c for c in f.cash if fd["start"] <= c["at"] <= cut]
    if any(_kind(c) == "other" for c in cash_today) or f.cash_truncated:
        return unavailable(row, "A balance entry since the reset is of a type TaliTrade doesn't recognise, so the start-of-day balance can't be rebuilt.")
    cash_sum = sum((c["amount"] or 0 for c in cash_today), Decimal(0))
    sod = f.balance - fd["net"] - cash_sum
    limit, limit_pct, missing = resolve(e, {"starting": size, "sod_balance": sod})
    if limit is None:
        return unavailable(row, "This limit is a percent of your starting size, which is not saved.", ACT_SIZE())
    used = max(sod - f.equity, Decimal(0))
    status, label, tone = _status_limit(used, limit)
    frac, ratio = _fraction(used, limit)
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "limit": money(limit), "limit_percent": pct(limit_pct),
                "used": money(used), "remaining": money(limit - used), "fraction": frac, "ratio_percent": ratio,
                "reference": {"kind": "sod_balance", "amount": money(sod)}, "floor": money(sod - limit), "current": money(f.equity), "measured_on": "equity",
                "realized_since_reset": money(fd["net"]), "open_pnl": money(f.equity - f.balance), "cash_since_reset": money(cash_sum) if cash_today else None,
                "closed_entries": fd["entries"], "next_reset": None, "stale": f.summary_stale or f.closed.stale,
                "delayed": bool(f.closed.synced_at and f.summary_at and f.closed.synced_at < f.summary_at - timedelta(seconds=90))})
    return row


def overall_row(f: Facts, fr: dict, size: Decimal | None) -> dict:
    e = fr["overall"]
    mode, ref = fr.get("overallMode"), fr.get("overallRef")
    row = {"id": "overall_drawdown", "group": "firm", "kind": "limit", "title": ("Trailing" if mode == "trailing" else "Static" if mode == "static" else "Overall") + " drawdown limit",
           "source": "Firm rule", "entered": entered(e), "mode": mode, "measured_on": ref}
    if mode not in ("static", "trailing"):
        return unavailable(row, "Needs whether the drawdown is static or trailing.", ACT_OVERALL())
    if ref not in ("balance", "equity"):
        return unavailable(row, "Needs whether your firm measures this on balance or equity.", ACT_OVERALL("Choose balance or equity"))
    if f.balance is None or f.equity is None:
        return unavailable(row, "Waiting for the balance and equity from your broker.", None, status="pending", label="Waiting for data")
    current = f.balance if ref == "balance" else f.equity
    if mode == "static":
        if size is None:
            return unavailable(row, "Needs the starting size your firm issued: a static drawdown is measured from it.", ACT_SIZE())
        limit, limit_pct, _ = resolve(e, {"starting": size})
        if limit is None:
            return unavailable(row, "This limit is a percent of your starting size, which is not saved.", ACT_SIZE())
        peak, peak_kind = size, "starting_size"
    else:
        if ref == "equity":
            return unavailable(row, "Trails the highest equity. The broker doesn't report it and open-trade highs can't be rebuilt from history, so this can't be measured yet.", ACT_OVERALL("Review drawdown rule"))
        peak, why = reconstructed_peak(f, size)
        if peak is None:
            return unavailable(row, why[0], why[1])
        peak_kind = "highest_balance"
        limit, limit_pct, _ = resolve(e, {"high_water": peak, "starting": size})
        if limit is None:
            return unavailable(row, "This limit's percent has no known reference.", ACT_OVERALL())
    used = max(peak - current, Decimal(0))
    status, label, tone = _status_limit(used, limit)
    frac, ratio = _fraction(used, limit)
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "limit": money(limit), "limit_percent": pct(limit_pct),
                "used": money(used), "remaining": money(current - (peak - limit)), "fraction": frac, "ratio_percent": ratio,
                "reference": {"kind": peak_kind, "amount": money(peak), "at": iso(f.peak["peak_at"]) if peak_kind == "highest_balance" and f.peak else None},
                "floor": money(peak - limit), "current": money(current), "stale": f.summary_stale,
                "notes": ["withdrawal_lowers_balance"] if any(_kind(c) == "withdrawal" for c in f.cash) else []})
    return row


def reconstructed_peak(f: Facts, size: Decimal | None) -> tuple[Decimal | None, tuple[str, dict | None]]:
    """Highest balance since the account opened, rebuilt from history - only when that history
    is complete and adds back exactly to the account's start (zero before the funding deposit,
    or the starting size when the funding deposit isn't in the ledger)."""
    created = f.account_created_at
    if created is None:
        return None, ("The broker didn't report when this account opened, so the highest balance since then can't be confirmed.", None)
    end = f.summary_at or f.now
    for cov in (f.closed, f.ledgers):
        state = cov.covers(created, end)
        if state != "verified":
            return None, ({"importing": "Trade history is still importing; the highest balance is known once it finishes.",
                           "not_imported": "Needs trade history back to when the account opened to find the highest balance.",
                           "gaps": "Part of the history could not be checked, so the highest balance can't be confirmed.",
                           "pending": "Waiting for the first history sync.",
                           "not_permitted": "This token can't read the history needed to find the highest balance."}[state],
                          ACT_IMPORT() if state == "not_imported" else None)
    pk = f.peak
    if not pk or pk["nulls"] or f.cash_truncated:
        return None, ("Some history entries have no amount, so the highest balance can't be rebuilt.", None)
    before = f.balance - pk["total"]
    if abs(before) > TOLERANCE and not (size is not None and abs(before - size) <= TOLERANCE):
        return None, (f"Imported history doesn't add back to this account's start ({money(before)} left over), so the highest balance isn't reliable.", ACT_SIZE() if size is None else None)
    peak = max(before, before + (pk["max_cum"] if pk["max_cum"] is not None else Decimal(0)), f.balance)
    return peak, ("", None)


# ── personal limits ──
def personal_rows(f: Facts, p: dict, size: Decimal | None) -> list[dict]:
    pr = p.get("personal") or {}
    rows = []
    if pr.get("sessionBudget"):
        rows.append(unavailable({"id": "session_budget", "group": "personal", "kind": "limit", "title": "Session loss budget", "source": "Your limit",
                                 "entered": entered(pr["sessionBudget"])},
                                "TaliTrade doesn't know when your trading sessions start and end, so session losses aren't measured.",
                                action("Edit personal limits", flow="personal", step="personal")))
    if pr.get("riskPerTrade"):
        rows.append(risk_row(f, pr["riskPerTrade"], size))
    return rows


def risk_row(f: Facts, e: dict, size: Decimal | None) -> dict:
    row = {"id": "risk_per_trade", "group": "personal", "kind": "limit", "title": "Risk per trade", "source": "Your limit", "entered": entered(e)}
    limit, limit_pct, _ = resolve(e, {"starting": size})
    if limit is None:
        return unavailable(row, "This limit is a percent of your starting size, which is not saved.", ACT_SIZE())
    row.update({"limit": money(limit), "limit_percent": pct(limit_pct)})
    if f.positions is None:
        return unavailable(row, "Waiting for open positions from your broker.", None, status="pending", label="Waiting for data")
    if not f.positions:
        row.update({"measurable": False, "status": "idle", "status_label": "No open trades", "tone": "neutral",
                    "reason": "Checked on open trades with a stop loss. None are open now."})
        return row
    measured, unmeasured = [], []
    for pos in f.positions:
        risk, why = position_risk(pos)
        label = pos.get("alias") or pos.get("symbol")
        if risk is None:
            unmeasured.append({"symbol": label, "reason": why})
        else:
            measured.append({"symbol": label, "risk": risk})
    row["unmeasured"] = unmeasured[:5]
    row["unmeasured_count"] = len(unmeasured)
    if not measured:
        reasons = {u["reason"] for u in unmeasured}
        return unavailable(row, "No open trade has a stop loss, so its risk can't be measured." if reasons == {"no_stop"}
                           else "Open trades can't be valued yet (no stop loss, or no price movement since entry).")
    worst = max(measured, key=lambda m: m["risk"])
    status, label, tone = _status_limit(worst["risk"], limit)
    frac, ratio = _fraction(worst["risk"], limit)
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "used": money(worst["risk"]),
                "remaining": money(limit - worst["risk"]), "fraction": frac, "ratio_percent": ratio, "worst_symbol": worst["symbol"],
                "measured_count": len(measured), "estimate": True,
                "trades_over": sum(1 for m in measured if m["risk"] > limit)})
    return row


def position_risk(pos: dict) -> tuple[Decimal | None, str | None]:
    o, c, sl, pnl = D(pos.get("open_price")), D(pos.get("current_price")), D(pos.get("sl_price")), D(pos.get("profit"))
    if sl is None or sl == 0:
        return None, "no_stop"
    if o is None or c is None or pnl is None or c == o:
        return None, "no_valuation"
    per_unit = pnl / (c - o)                # money per price unit, side already in the sign of pnl
    at_stop = per_unit * (sl - o)
    return max(-at_stop, Decimal(0)).quantize(CENT, rounding=ROUND_HALF_EVEN), None


# ── Challenge / payout tile ──
def overview(p: dict, rows: list[dict]) -> dict:
    if p.get("status") != "saved" and not p.get("type"):
        return {"state": "setup", "action": action("Set up account", flow="basics")}
    by = {r["id"]: r for r in rows}
    for r in rows:
        for a in r.get("also", []):
            by.setdefault(a["id"], r)
    kind = p.get("type")
    if kind == "evaluation":
        r = by.get("phase_target")
        if not r:
            return {"state": "no_target", "label": "Evaluation", "action": action("Set phase target", flow="phase")}
        return _tile(r, r["title"])
    if kind == "master":
        msr = p.get("master") or {}
        for rid, label in (("payout_target", "Your payout target"), ("firm_min", "Firm minimum for payout"), ("growth_target", "Growth target")):
            if rid in by:
                return _tile(by[rid], label)
        goal = (msr.get("goal") or {}).get("id")
        if goal == "grow":
            return {"state": "no_target", "label": "Grow the account", "action": action("Set growth amount", flow="growth", step="growthTarget")}
        if goal == "payout":
            return {"state": "no_target", "label": "Earn a payout", "action": action("Set payout target", flow="payout", step="payoutTarget")}
        return {"state": "no_target", "label": "Master / funded", "action": action("Set a target", flow="payout", step="payoutTarget")}
    return {"state": "no_type", "action": action("Choose account type", flow="basics", step="type")}


def _tile(r: dict, label: str) -> dict:
    out = {"state": r["status"] if r.get("measurable") else "unavailable", "label": label, "row": r["id"], "status_label": r["status_label"], "tone": r["tone"]}
    if r.get("measurable"):
        out.update(target=r["target"], current=r["current"], ratio_percent=r["ratio_percent"], fraction=r["fraction"],
                   baseline=r["baseline"], remaining=r["remaining"])
    else:
        out.update(reason=r.get("reason"), action=r.get("action"))
    return out
