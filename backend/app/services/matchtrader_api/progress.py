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
"unavailable" with the reason and the setup action that would supply it.

Cash rule used throughout: deposits and withdrawals change the balance but are never trading
results. Progress and loss figures take them back out, and when the cash history needed to do
that is not imported, the figure is "unconfirmed" rather than presented as settled.
``DEFINITIONS`` below is returned with the figures and is the contract the tests check.
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
FUNDING_WINDOW = timedelta(days=2)   # the funding deposit is the first deposit within 2 days of the account opening
MIN_MOVE = Decimal("0.0002")         # risk valuation needs the price at least 0.02 % away from entry
MIN_PNL = Decimal("2")               # ...and at least 2.00 of P&L, so cent rounding stays under 0.25 %
HISTORY_LAG_GRACE = 60               # seconds of history-sync delay tolerated on top of the history interval

DEFINITIONS = {
    "daily_pnl": "Sum of the broker's net result (commission and swap already included, never subtracted again) of every closing entry whose close time falls on the current calendar day in the dashboard time zone. A position closed in parts counts each part once; entries are stored once per broker uid, so a re-sent trade is not counted twice. Open positions and deposits/withdrawals are not included.",
    "phase_target": "Balance minus the phase starting balance, minus deposits and plus withdrawals made after that start. Phase 1 (or a one-phase evaluation) starts at the starting size the trader entered; later phases need the starting balance the trader confirmed for that phase. Open trades are not counted. Confirmed only when deposits and withdrawals are imported back to the start.",
    "payout_target": "Gross profit: balance minus the starting size - or minus the cycle start the trader confirmed after the latest withdrawal - with deposits after that point taken out. It is profit before the firm's split; with a split entered, the trader's share is shown separately. Reaching it does not establish payout eligibility.",
    "daily_loss": "Today's trading loss = -(net results closed since the reset + open P&L now). It equals start-of-day balance minus current equity with deposits and withdrawals taken out, so cash never counts as a gain or a loss. The day starts at the trader's reset time in the reset time zone. A limit measured from start-of-day equity is not measured: equity at the reset time is not recorded.",
    "static_drawdown": "Starting size minus the current balance or equity (as the rule says) with deposits and withdrawals after funding taken out. Floor = starting size minus the limit.",
    "trailing_drawdown": "Highest balance minus current balance, both with deposits and withdrawals taken out. The highest balance is rebuilt from closed trades in time order, and only when trade and cash history are imported back to the account opening and add back exactly to the funding amount. Equity peaks cannot be rebuilt, so equity-trailing rules are not measured.",
    "risk_per_trade": "Loss if an open position hits its stop, before commission and swap. Value per price unit = the broker's current profit / (current price - open price). Exact when the instrument is priced in the account currency; otherwise an estimate at today's conversion rate. Not valued without a stop or within 0.02 % of entry.",
    "session_budget": "Not measured: TaliTrade does not know when a trading session starts and ends.",
}
ROW_DEFINITION = {"phase_target": "phase_target", "payout_target": "payout_target", "firm_min": "payout_target", "growth_target": "payout_target",
                  "daily_loss": "daily_loss", "overall_drawdown": None, "risk_per_trade": "risk_per_trade", "session_budget": "session_budget"}


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
    quotes: dict[str, str] = field(default_factory=dict)      # symbol -> quote currency, when the broker listed it
    closed: Coverage = field(default_factory=Coverage)
    ledgers: Coverage = field(default_factory=Coverage)
    day: dict = field(default_factory=dict)          # aggregates for the dashboard day
    firm_day: dict | None = None                     # aggregates since the firm reset (to summary time)
    all_trades: dict = field(default_factory=dict)
    cash: list[dict] = field(default_factory=list)   # non-trade ledger rows, oldest first
    cash_truncated: bool = False
    peak: dict | None = None                         # trade-only running totals, when asked for
    profile: dict | None = None
    setup_enabled: bool = False
    history_interval: int = 120


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
    from app.services.matchtrader_api import config
    from app.services.matchtrader_api.read_models import _freshness
    now = (await conn.execute(text("SELECT NOW()"))).scalar()
    fresh = _freshness(await read_model._states(conn, connection_id, user_id), c["capabilities"] or {}, now)
    f = Facts(now=now, currency=c["account_currency"], account_created_at=c["account_created_at"], setup_enabled=setup_enabled,
              history_interval=config.history_interval_seconds())

    if fresh["summary"]["last_success_at"]:
        s = (await conn.execute(text("SELECT balance, equity, profit, fetched_at FROM mtapi_account_summary WHERE connection_id = :id"), params)).mappings().first()
        if s:
            f.balance, f.equity, f.floating, f.summary_at = D(s["balance"]), D(s["equity"]), D(s["profit"]), s["fetched_at"]
            f.summary_stale = bool(fresh["summary"].get("is_stale"))
    if fresh["positions"]["last_success_at"]:
        rows = (await conn.execute(text("""SELECT external_id, symbol, alias, side, volume, open_price, current_price, sl_price, profit
                                            FROM mtapi_positions WHERE connection_id = :id ORDER BY open_time NULLS LAST, external_id"""), params)).mappings().all()
        f.positions = [dict(r) for r in rows]
        if f.positions and (await conn.execute(text("SELECT to_regclass('mtapi_market_symbols') IS NOT NULL"))).scalar():
            q = (await conn.execute(text("""SELECT symbol, quote_currency FROM mtapi_market_symbols
                                             WHERE connection_id = :id AND quote_currency IS NOT NULL"""), params)).all()
            f.quotes = {r[0]: r[1].upper() for r in q}

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
        # Trade results only, in close-time order (uid breaks ties): cash is taken out in Python.
        p = (await conn.execute(text("""
            WITH run AS (
                SELECT close_time AS t, net_profit AS a,
                       SUM(net_profit) OVER (ORDER BY close_time, uid ROWS UNBOUNDED PRECEDING) AS cum
                FROM mtapi_closed_positions WHERE connection_id = :id AND close_time <= :cut)
            SELECT COALESCE(SUM(a), 0) AS total, COUNT(*) FILTER (WHERE a IS NULL) AS nulls, COUNT(*) AS events, MAX(cum) AS max_cum,
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


# ───────────────────────────── cash ─────────────────────────────
def _kind(entry: dict) -> str:
    t = entry["type"]
    if "DEPOSIT" in t:
        return "deposit"
    if "WITHDRAW" in t:
        return "withdrawal"
    return "other"


@dataclass
class Cash:
    """The account's non-trade balance changes, split into the funding deposit and later flows."""
    funding: dict | None
    flows: list[dict]
    truncated: bool

    def after(self, since: datetime | None) -> list[dict]:
        return [c for c in self.flows if since is None or c["at"] > since]


def cash_of(f: Facts) -> Cash:
    created = f.account_created_at
    funding = None
    for c in f.cash:
        if _kind(c) == "deposit" and (c["amount"] or 0) > 0 and created is not None and c["at"] <= created + FUNDING_WINDOW:
            funding = c
            break
    return Cash(funding, [c for c in f.cash if c is not funding], f.cash_truncated)


def cash_confirmed(f: Facts, cash: Cash, since: datetime | None) -> tuple[bool, str | None]:
    """Whether every deposit/withdrawal after ``since`` is known: ledger history imported from
    ``since`` (or the account opening) to the balance time, nothing truncated, no unknown types."""
    start = since or f.account_created_at
    if start is None:
        return False, "opening_unknown"
    state = f.ledgers.covers(start, f.summary_at or f.now)
    if state != "verified":
        return False, state
    if cash.truncated:
        return False, "too_many"
    if any(_kind(c) == "other" for c in cash.after(since)):
        return False, "unknown_type"
    return True, None


def flow_sum(entries: list[dict]) -> Decimal:
    return sum((c["amount"] or Decimal(0) for c in entries), Decimal(0))


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


UNCONFIRMED_WHY = {
    "importing": "Deposit and withdrawal history is still importing.",
    "not_imported": "Deposit and withdrawal history isn't imported back to this baseline yet.",
    "gaps": "Part of the deposit and withdrawal history could not be checked.",
    "pending": "Deposit and withdrawal history hasn't synced yet.",
    "not_permitted": "This token can't read deposits and withdrawals.",
    "too_many": "There are too many deposit and withdrawal entries to check here.",
    "unknown_type": "A balance entry of a type TaliTrade doesn't recognise was found.",
    "opening_unknown": "The broker didn't report when the account opened.",
    "phase_cash": "Cash moved during the evaluation; TaliTrade can't tell whether it was before this phase started.",
}


def unconfirm(row: dict, why: str) -> dict:
    """Figures are shown, but no settled status: the cash history behind them isn't complete."""
    row.update({"status": "unconfirmed", "status_label": "Unconfirmed", "tone": "neutral", "unconfirmed_reason": UNCONFIRMED_WHY.get(why, why)})
    if why == "not_imported" and "action" not in row:
        row["action"] = ACT_IMPORT()
    return row


def build(f: Facts, tz_name: str) -> dict:
    out = {"currency": f.currency, "server_time": iso(f.now), "time_zone": tz_name, "definitions": DEFINITIONS,
           "daily_pnl": daily_pnl(f, tz_name), "rows": [], "labels": [], "overview": None, "setup": "disabled"}
    if not f.setup_enabled:
        return out
    p = f.profile or {}
    out["setup"] = "saved" if p.get("status") == "saved" else (p.get("status") or "new")
    rows, labels = rows_for(f, p)
    for r in rows:
        key = ROW_DEFINITION.get(r["id"])
        if r["id"] == "overall_drawdown":
            key = "trailing_drawdown" if r.get("mode") == "trailing" else "static_drawdown"
        if key:
            r["definition"] = key
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
    cash = cash_of(f)
    opened = cash.funding["at"] if cash.funding else f.account_created_at

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
            amount, percent, _ = resolve(target, {"starting": size})
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
                flows = cash.after(opened)
                r = target_row(row, f.balance - start - flow_sum(flows), amount, percent, start, start_note, flows, f)
                ok, why = cash_confirmed(f, cash, opened)
                if not ok:
                    unconfirm(r, why)
                elif start_note == "confirmed_phase_start" and flows:
                    unconfirm(r, "phase_cash")
                rows.append(r)
    # ── Master: payout target, firm minimum, growth target ──
    if kind == "master":
        msr = p.get("master") or {}
        withdrawals = [c for c in cash.flows if _kind(c) == "withdrawal"]
        last_w = withdrawals[-1] if withdrawals else None
        cyc = msr.get("cycleStart") or {}
        start, start_note, since = size, "starting_size", opened
        cycle_needed = False
        if last_w is not None:
            after = _parse_dt(cyc.get("after"))
            if D(cyc.get("amount")) and after is not None and abs((after - last_w["at"]).total_seconds()) < 1:
                start, start_note, since = D(cyc["amount"]), "confirmed_cycle_start", last_w["at"]
            else:
                cycle_needed = True
        split = D(msr.get("profitSplit"))
        defs = [("payoutTarget", "payout_target", "Your payout target", "Your target"),
                ("firmMin", "firm_min", "Firm minimum profit for payout", "Firm rule"),
                ("growthTarget", "growth_target", "Growth target", "Your target")]
        made: list[dict] = []
        for key, rid, title, source in defs:
            e = msr.get(key)
            if not e:
                continue
            row = {"id": rid, "group": "target", "kind": "target", "title": title, "source": source, "entered": entered(e),
                   "eligibility_note": key != "growthTarget", "basis": "gross_profit"}
            amount, percent, _ = resolve(e, {"starting": size})
            if size is None and start_note == "starting_size":
                made.append(unavailable(row, "Needs the starting size your firm issued: profit is measured from it.", ACT_SIZE()))
            elif amount is None:
                made.append(unavailable(row, "This target is a percent of your starting size, which is not saved.", ACT_SIZE()))
            elif cycle_needed:
                made.append(unavailable(row, f"A withdrawal of {money(abs(last_w['amount'] or 0))} on {last_w['at'].date().isoformat()} is in this account's history. TaliTrade can't tell where the current payout cycle started.",
                                        {**action("Confirm cycle start", flow="cycle", step="cycleStart"), "after": iso(last_w["at"]), "withdrawal": money(abs(last_w["amount"] or 0))}))
            elif f.balance is None:
                made.append(unavailable(row, "Waiting for the first balance from your broker.", None, status="pending", label="Waiting for data"))
            else:
                flows = cash.after(since)
                r = target_row(row, f.balance - start - flow_sum(flows), amount, percent, start, start_note, flows, f)
                if split and key == "payoutTarget":
                    gross = D(r["current"])
                    r["profit_split"] = pct(split)
                    r["your_share"] = money(max(gross, Decimal(0)) * split / 100)
                    r["target_share"] = money(amount * split / 100)
                ok, why = cash_confirmed(f, cash, since if start_note == "confirmed_cycle_start" else None)
                if not ok:
                    unconfirm(r, why)
                made.append(r)
        rows.extend(combine(made))

    rows.extend(firm_rows(f, p, size, cash, opened))
    rows.extend(personal_rows(f, p, size))
    labels = []
    pr = p.get("personal") or {}
    if pr.get("style"):
        labels.append({"title": "Trading style", "value": pr["style"]})
    return rows, labels


def target_row(row: dict, progress: Decimal, target: Decimal, target_pct: Decimal | None, start: Decimal, start_note: str,
               flows: list[dict], f: Facts) -> dict:
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
                "measured_on": "balance", "open_pnl_excluded": money(f.floating), "notes": [],
                "balance_at": iso(f.summary_at), "stale": f.summary_stale})
    if flows:
        row["notes"].append("cash_excluded")
        row["cash_excluded"] = money(flow_sum(flows))
    return row


def combine(rows: list[dict]) -> list[dict]:
    """Rows measuring the same thing to the same amount become one row that keeps every meaning."""
    out: list[dict] = []
    for r in rows:
        twin = next((o for o in out if (o.get("measurable") and r.get("measurable") and o["target"] == r["target"] and o["baseline"] == r["baseline"])
                     or (not o.get("measurable") and not r.get("measurable") and o.get("reason") == r.get("reason")
                         and (o.get("action") or {}).get("flow") == (r.get("action") or {}).get("flow"))), None)
        if twin:
            twin.setdefault("also", []).append({"id": r["id"], "title": r["title"], "source": r["source"], "entered": r["entered"]})
            twin["eligibility_note"] = twin.get("eligibility_note") or r.get("eligibility_note")
        else:
            out.append(r)
    return out


# ── firm rules ──
def firm_rows(f: Facts, p: dict, size: Decimal | None, cash: Cash, opened: datetime | None) -> list[dict]:
    fr = p.get("firmRules") or {}
    rows = []
    if fr.get("dailyLoss"):
        rows.append(daily_loss_row(f, fr, size))
    if fr.get("overall"):
        rows.append(overall_row(f, fr, size, cash, opened))
    return rows


ACT_DAILY = lambda label="Finish daily loss rule": action(label, flow="firm", step="firmDaily")
ACT_OVERALL = lambda label="Finish drawdown rule": action(label, flow="firm", step="firmOverall")
COVER_WHY = {"importing": "Trade history since the reset is still importing.", "gaps": "Part of today's trade history could not be checked.",
             "not_imported": "Trade history doesn't reach back to the reset yet.", "pending": "Waiting for the first trade sync.",
             "not_permitted": "This token can't read closed trades."}


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
        return unavailable(row, "Counted from start-of-day equity. TaliTrade doesn't record equity at your reset time, so this can't be measured yet.", ACT_DAILY("Review daily rule"))
    fd = f.firm_day
    if f.balance is None or f.floating is None or fd is None:
        return unavailable(row, "Waiting for the balance and open P&L from your broker.", None, status="pending", label="Waiting for data")
    row["day_start"] = iso(fd["start"])
    cut = fd["cut"]
    closed = f.closed.covers(fd["start"], cut)
    if closed != "verified":
        return unavailable(row, COVER_WHY[closed], ACT_IMPORT() if closed == "not_imported" else None,
                           status="pending" if closed in ("importing", "pending") else "unavailable",
                           label="Updating" if closed in ("importing", "pending") else "Not measured")
    lag = (f.summary_at - f.closed.synced_at).total_seconds() if f.summary_at and f.closed.synced_at else 0
    if lag > f.history_interval + HISTORY_LAG_GRACE:
        return unavailable(row, "Closed trades are syncing behind the balance; today's loss is shown once they catch up.", None, status="pending", label="Updating")
    if fd["unvalued"]:
        return unavailable(row, "A trade closed since the reset has no net result from the broker.")
    used = max(-(fd["net"] + f.floating), Decimal(0))
    # The start-of-day balance itself needs every deposit/withdrawal since the reset.
    cash_today = [c for c in f.cash if fd["start"] <= c["at"] <= cut]
    ledg = f.ledgers.covers(fd["start"], cut)
    sod = None
    if ledg == "verified" and not f.cash_truncated:
        sod = f.balance - fd["net"] - flow_sum(cash_today)
    limit, limit_pct, missing = resolve(e, {"starting": size, "sod_balance": sod})
    if limit is None:
        if missing == "starting":
            return unavailable(row, "This limit is a percent of your starting size, which is not saved.", ACT_SIZE())
        return unavailable(row, "This limit is a percent of today's start-of-day balance, which needs today's deposits and withdrawals imported.",
                           ACT_IMPORT() if ledg == "not_imported" else None)
    status, label, tone = _status_limit(used, limit)
    frac, ratio = _fraction(used, limit)
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "limit": money(limit), "limit_percent": pct(limit_pct),
                "used": money(used), "remaining": money(limit - used), "fraction": frac, "ratio_percent": ratio,
                "reference": {"kind": "sod_balance", "amount": money(sod)} if sod is not None else None,
                "floor": money(sod - limit) if sod is not None else None, "measured_on": "equity",
                "realized_since_reset": money(fd["net"]), "open_pnl": money(f.floating),
                "cash_since_reset": money(flow_sum(cash_today)) if cash_today else None,
                "closed_entries": fd["entries"], "trades_synced_at": iso(f.closed.synced_at), "stale": f.summary_stale or f.closed.stale})
    return row


def overall_row(f: Facts, fr: dict, size: Decimal | None, cash: Cash, opened: datetime | None) -> dict:
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
    flows = cash.after(opened)
    current = (f.balance if ref == "balance" else f.equity) - flow_sum(flows)       # cash taken out
    confirmed, why = cash_confirmed(f, cash, None)
    if mode == "static":
        if size is None:
            return unavailable(row, "Needs the starting size your firm issued: a static drawdown is measured from it.", ACT_SIZE())
        limit, limit_pct, _ = resolve(e, {"starting": size})
        if limit is None:
            return unavailable(row, "This limit is a percent of your starting size, which is not saved.", ACT_SIZE())
        peak, peak_kind, peak_at = size, "starting_size", None
    else:
        if ref == "equity":
            return unavailable(row, "Trails the highest equity. The broker doesn't report it and open-trade highs can't be rebuilt from history, so this can't be measured yet.", ACT_OVERALL("Review drawdown rule"))
        peak, why_peak = reconstructed_peak(f, size, cash)
        if peak is None:
            return unavailable(row, why_peak[0], why_peak[1])
        peak_kind, peak_at = "highest_balance", f.peak["peak_at"] if f.peak else None
        limit, limit_pct, _ = resolve(e, {"high_water": peak, "starting": size})
        if limit is None:
            return unavailable(row, "This limit's percent has no known reference.", ACT_OVERALL())
    used = max(peak - current, Decimal(0))
    status, label, tone = _status_limit(used, limit)
    frac, ratio = _fraction(used, limit)
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "limit": money(limit), "limit_percent": pct(limit_pct),
                "used": money(used), "remaining": money(current - (peak - limit)), "fraction": frac, "ratio_percent": ratio,
                "reference": {"kind": peak_kind, "amount": money(peak), "at": iso(peak_at)},
                "floor": money(peak - limit), "current": money(current), "stale": f.summary_stale, "notes": []})
    if flows:
        row["notes"].append("cash_excluded")
        row["cash_excluded"] = money(flow_sum(flows))
    if not confirmed:
        unconfirm(row, why)
    return row


def reconstructed_peak(f: Facts, size: Decimal | None, cash: Cash) -> tuple[Decimal | None, tuple[str, dict | None]]:
    """Highest cash-adjusted balance since the account opened, rebuilt from closed trades in time
    order. Coverage is checked first and separately: totals that happen to add up never stand in
    for imported history. Then the history must add back to the funding amount (or, with no
    funding entry, to the starting size) to the cent."""
    created = f.account_created_at
    if created is None:
        return None, ("The broker didn't report when this account opened, so the highest balance since then can't be confirmed.", None)
    end = f.summary_at or f.now
    for cov in (f.closed, f.ledgers):
        state = cov.covers(created, end)
        if state != "verified":
            return None, ({"importing": "Trade history is still importing; the highest balance is known once it finishes.",
                           "not_imported": "Needs trade and cash history back to when the account opened to find the highest balance.",
                           "gaps": "Part of the history could not be checked, so the highest balance can't be confirmed.",
                           "pending": "Waiting for the first history sync.",
                           "not_permitted": "This token can't read the history needed to find the highest balance."}[state],
                          ACT_IMPORT() if state == "not_imported" else None)
    lag = (f.summary_at - min(f.closed.synced_at, f.ledgers.synced_at)).total_seconds() if f.summary_at else 0
    if lag > f.history_interval + HISTORY_LAG_GRACE:
        return None, ("Trade history is syncing behind the balance; the highest balance is shown once it catches up.", None)
    pk = f.peak
    if not pk or pk["nulls"] or cash.truncated or any(_kind(c) == "other" for c in cash.flows):
        return None, ("Some history entries have no amount or an unrecognised type, so the highest balance can't be rebuilt.", None)
    start = f.balance - flow_sum(cash.flows) - pk["total"]            # balance before the first trade, cash taken out
    expected = D(cash.funding["amount"]) if cash.funding else size
    if expected is None or abs(start - expected) > TOLERANCE:
        return None, (f"Imported history doesn't add back to this account's {'funding deposit' if cash.funding else 'starting size'} "
                      f"({money(start)} vs {money(expected)}), so the highest balance isn't reliable.", ACT_SIZE() if expected is None else None)
    peak = max(start, start + (pk["max_cum"] if pk["max_cum"] is not None else Decimal(0)))
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


RISK_WHY = {"no_stop": "no stop loss", "near_entry": "too close to entry to value", "no_pnl": "no P&L from the broker yet",
            "no_side": "the broker didn't report long or short"}


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
        risk, why, exact = position_risk(pos, f.currency, f.quotes)
        label = pos.get("alias") or pos.get("symbol")
        if risk is None:
            unmeasured.append({"symbol": label, "reason": why, "reason_text": RISK_WHY[why]})
        else:
            measured.append({"symbol": label, "risk": risk, "exact": exact, "note": why})
    row["positions"] = [{"symbol": m["symbol"], "risk": money(m["risk"]), "exact": m["exact"], "stop_in_profit": m["note"] == "stop_in_profit"} for m in measured][:8]
    row["unmeasured"] = unmeasured[:8]
    row["unmeasured_count"] = len(unmeasured)
    if not measured:
        reasons = sorted({u["reason_text"] for u in unmeasured})
        return unavailable(row, "Risk unavailable: " + "; ".join(reasons) + ".", None, label="Risk unavailable")
    worst = max(measured, key=lambda m: m["risk"])
    frac, ratio = _fraction(worst["risk"], limit)
    over = [m for m in measured if m["risk"] > limit]
    exact_over = [m for m in over if m["exact"]]
    if exact_over:
        status, label, tone = _status_limit(max(m["risk"] for m in exact_over), limit)
    elif over:
        status, label, tone = "estimate_over", "Estimate over limit", "amber"
    elif any(not m["exact"] and m["risk"] == limit for m in measured):
        status, label, tone = "estimate_at", "Estimate at limit", "amber"
    elif all(m["exact"] for m in measured) and not unmeasured:
        status, label, tone = _status_limit(worst["risk"], limit)
    elif not all(m["exact"] for m in measured):
        status, label, tone = "estimate", "Estimate only", "neutral"
    else:
        status, label, tone = "partial", "Partly measured", "neutral"
    row.update({"measurable": True, "status": status, "status_label": label, "tone": tone, "used": money(worst["risk"]),
                "remaining": money(limit - worst["risk"]), "fraction": frac, "ratio_percent": ratio, "worst_symbol": worst["symbol"],
                "measured_count": len(measured), "estimate": not worst["exact"], "trades_over": len(over)})
    return row


def position_risk(pos: dict, account_ccy: str | None, quotes: dict[str, str]) -> tuple[Decimal | None, str | None, bool]:
    """(loss at the stop before commission and swap, reason or note, exact?)."""
    o, c, sl, pnl = D(pos.get("open_price")), D(pos.get("current_price")), D(pos.get("sl_price")), D(pos.get("profit"))
    side = (pos.get("side") or "").upper()
    if sl is None or sl == 0:
        return None, "no_stop", False
    if side in ("BUY", "SELL") and o is not None:
        if (side == "BUY" and sl >= o) or (side == "SELL" and sl <= o):
            return Decimal("0.00"), "stop_in_profit", True                # the stop locks in breakeven or better
    elif side not in ("BUY", "SELL"):
        return None, "no_side", False
    if o is None or c is None or pnl is None:
        return None, "no_pnl", False
    if c == o or abs(c - o) / o < MIN_MOVE or abs(pnl) < MIN_PNL:
        return None, "near_entry", False
    per_unit = pnl / (c - o)                                              # money per price unit; the sign carries long/short
    if per_unit == 0 or (side == "BUY" and per_unit < 0) or (side == "SELL" and per_unit > 0):
        return None, "no_pnl", False                                      # P&L disagrees with the price move: don't value it
    at_stop = per_unit * (sl - o)
    quote = quotes.get(pos.get("symbol") or "")
    exact = bool(quote and account_ccy and quote == account_ccy.upper())
    return max(-at_stop, Decimal(0)).quantize(CENT, rounding=ROUND_HALF_EVEN), None, exact


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
        if r.get("action"):
            out["action"] = r["action"]
    else:
        out.update(reason=r.get("reason"), action=r.get("action"))
    return out
