"""Owner-scoped read models for showing imported Match-Trader data in the dashboard.

Reads PostgreSQL only: nothing here contacts the broker. Every query repeats the ownership
predicate (matching user, not disconnected) in SQL, so a row can never be returned on the
strength of an earlier check. Only allow-listed fields are returned: no ciphertext, key
versions, generations, lease fields, raw provider payloads, revisions or profile PII.

Money is serialized as decimal strings and times as UTC ISO-8601, so the browser never does
float arithmetic on stored values. "Unknown" is null and is kept distinct from zero/empty.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable

from sqlalchemy import text

from app.services.matchtrader_api import config

MAX_TRADE_PAGE = 100
LIVE_RESOURCES = ("summary", "positions", "pending_orders")
ALL_RESOURCES = LIVE_RESOURCES + ("closed_positions", "ledgers")
_OWNED = "c.id = :id AND c.user_id = :user AND c.status <> 'disconnected'"


class NotFound(Exception):
    """Unknown, other-owned or disconnected connection. Callers answer a generic 404."""


class BadCursor(Exception):
    pass


class BadRange(Exception):
    """An analytics range or time zone that cannot be used."""


MAX_ANALYTICS_DAYS = 3700   # about ten years of daily rows


def _default_engine():
    from app.core.database import engine

    return engine


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text_value = format(value, "f")
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return text_value or "0"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ms_iso(value: int | None) -> str | None:
    return _iso(datetime.fromtimestamp(value / 1000, tz=timezone.utc)) if value is not None else None


def mask_login(login: str) -> str:
    login = str(login or "")
    return "…" + login[-4:] if len(login) > 4 else "…"


def account_key(connection_id: int) -> str:
    return f"mtapi:{int(connection_id)}"


def encode_cursor(close_time: datetime, uid: str) -> str:
    raw = json.dumps([close_time.astimezone(timezone.utc).isoformat(), uid]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        if not cursor or len(cursor) > 400:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        when, uid = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        parsed = datetime.fromisoformat(when)
        if parsed.tzinfo is None or not isinstance(uid, str) or not uid or len(uid) > 200:
            raise ValueError
        return parsed, uid
    except Exception:
        raise BadCursor() from None


def _stale_after_seconds(resource: str) -> int:
    base = config.live_interval_seconds() if resource in LIVE_RESOURCES else config.history_interval_seconds()
    return max(90, base * 3)


def _freshness(states: dict[str, dict], capabilities: dict, now: datetime) -> dict:
    out = {}
    for resource in ALL_RESOURCES:
        state = states.get(resource)
        last_success = state["last_success_at"] if state else None
        if capabilities.get(resource) is False:
            status = "not_permitted"
        elif last_success is None:
            status = "never_synced" if not (state and state["last_error_code"]) else "failed"
        elif (now - last_success).total_seconds() > _stale_after_seconds(resource):
            status = "stale"
        elif state["last_error_code"]:
            status = "degraded"       # last attempt failed, but the stored data is still recent
        else:
            status = "fresh"
        out[resource] = {
            "status": status,
            "last_success_at": _iso(last_success),
            "last_attempt_at": _iso(state["last_attempt_at"]) if state else None,
            "last_error": state["last_error_code"] if state else None,
            "is_stale": status in ("stale", "failed"),
        }
    return out


def _display_state(status: str, freshness: dict) -> str:
    if status != "active":
        return status
    if freshness["summary"]["last_success_at"] is None:
        return "first_sync_pending"
    if freshness["summary"]["is_stale"] or freshness["positions"]["is_stale"]:
        return "delayed"
    return "ready"


def _connection_public(row, freshness: dict) -> dict:
    masked = mask_login(row["external_login"])
    broker = row["broker_name"] or "Match-Trader"
    return {
        "id": row["id"], "account_key": account_key(row["id"]), "connector_type": "matchtrader_api", "data_source": "matchtrader_api",
        "broker_name": broker, "display_label": f"{broker} · {masked}", "masked_login": masked,
        "currency": row["account_currency"], "provider_account_type": row["account_type"],
        "status": row["status"], "status_detail": row["status_detail"], "display_state": _display_state(row["status"], freshness),
        "capabilities": row["capabilities"] or {}, "ingestion_mode": row["ingestion_mode"], "created_at": _iso(row["created_at"]),
    }


def card_for_new_connection(row: dict) -> dict:
    """The switcher card for a connection that was just created: nothing synced yet, so no query is needed."""
    card = _connection_public(row, _freshness({}, row.get("capabilities") or {}, datetime.now(timezone.utc)))
    card["balance"], card["last_synced_at"] = None, None
    return card


class ReadModels:
    def __init__(self, engine_getter: Callable[[], Any] = _default_engine):
        self._engine_getter = engine_getter

    async def _states(self, conn, connection_id: int, user_id: str) -> dict[str, dict]:
        rows = (await conn.execute(text(f"""
            SELECT s.resource, s.last_attempt_at, s.last_success_at, s.last_error_code
            FROM mtapi_sync_state s JOIN mtapi_connections c ON c.id = s.connection_id WHERE {_OWNED}
        """), {"id": connection_id, "user": user_id})).mappings().all()
        return {r["resource"]: dict(r) for r in rows}

    async def list_owned(self, user_id: str) -> list[dict]:
        """Cards for the account switcher. Restores connections after login/reload.
        Two queries however many connections there are (the page loads this on every start)."""
        engine = self._engine_getter()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            rows = (await conn.execute(text("""
                SELECT c.id, c.external_login, c.broker_name, c.account_currency, c.account_type, c.status, c.status_detail,
                       c.capabilities, c.ingestion_mode, c.created_at, a.balance, NOW() AS server_now
                FROM mtapi_connections c LEFT JOIN mtapi_account_summary a ON a.connection_id = c.id
                WHERE c.user_id = :user AND c.status <> 'disconnected' ORDER BY c.id
            """), {"user": user_id})).mappings().all()
            if not rows:
                return []
            states: dict[int, dict[str, dict]] = {}
            for s in (await conn.execute(text("""
                SELECT s.connection_id, s.resource, s.last_attempt_at, s.last_success_at, s.last_error_code
                FROM mtapi_sync_state s JOIN mtapi_connections c ON c.id = s.connection_id
                WHERE c.user_id = :user AND c.status <> 'disconnected'
            """), {"user": user_id})).mappings().all():
                states.setdefault(s["connection_id"], {})[s["resource"]] = dict(s)
        out = []
        for row in rows:
            freshness = _freshness(states.get(row["id"], {}), row["capabilities"] or {}, row["server_now"])
            card = _connection_public(row, freshness)
            card["balance"] = _money(row["balance"]) if row["balance"] is not None and freshness["summary"]["last_success_at"] else None
            card["last_synced_at"] = freshness["summary"]["last_success_at"]
            out.append(card)
        return out

    async def read_owned_snapshot(self, connection_id: int, user_id: str) -> dict:
        """One coherent view of everything persisted for a connection (single repeatable-read transaction)."""
        engine = self._engine_getter()
        params = {"id": connection_id, "user": user_id}
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            row = (await conn.execute(text(f"""
                SELECT c.id, c.external_login, c.broker_name, c.account_currency, c.account_type, c.status, c.status_detail,
                       c.capabilities, c.ingestion_mode, c.created_at, c.account_created_at
                FROM mtapi_connections c WHERE {_OWNED}
            """), params)).mappings().first()
            if not row:
                raise NotFound()
            now = (await conn.execute(text("SELECT NOW()"))).scalar()
            capabilities = row["capabilities"] or {}
            freshness = _freshness(await self._states(conn, connection_id, user_id), capabilities, now)

            summary = None
            if freshness["summary"]["last_success_at"]:   # unknown stays null: never zeros
                s = (await conn.execute(text(f"""
                    SELECT a.balance, a.credit, a.equity, a.margin, a.free_funds, a.margin_level, a.profit, a.fetched_at
                    FROM mtapi_account_summary a JOIN mtapi_connections c ON c.id = a.connection_id WHERE {_OWNED}
                """), params)).mappings().first()
                if s:
                    summary = {k: _money(s[k]) for k in ("balance", "credit", "equity", "margin", "free_funds", "margin_level", "profit")}
                    summary["fetched_at"] = _iso(s["fetched_at"])

            positions = None
            if freshness["positions"]["last_success_at"]:   # null = never fetched; [] = fetched and empty
                rows = (await conn.execute(text(f"""
                    SELECT p.external_id, p.symbol, p.alias, p.side, p.volume, p.open_price, p.current_price, p.commission, p.swap,
                           p.profit, p.net_profit, p.margin, p.open_time, p.tp_price, p.sl_price
                    FROM mtapi_positions p JOIN mtapi_connections c ON c.id = p.connection_id WHERE {_OWNED}
                    ORDER BY p.open_time DESC NULLS LAST, p.external_id
                """), params)).mappings().all()
                positions = [{
                    "id": r["external_id"], "symbol": r["symbol"], "alias": r["alias"], "side": r["side"], "volume": _money(r["volume"]),
                    "open_price": _money(r["open_price"]), "current_price": _money(r["current_price"]), "commission": _money(r["commission"]),
                    "swap": _money(r["swap"]), "profit": _money(r["profit"]), "net_profit": _money(r["net_profit"]), "margin": _money(r["margin"]),
                    "open_time": _iso(r["open_time"]), "tp_price": _money(r["tp_price"]), "sl_price": _money(r["sl_price"]),
                } for r in rows]

            pending = None
            if freshness["pending_orders"]["last_success_at"]:
                rows = (await conn.execute(text(f"""
                    SELECT o.external_id, o.symbol, o.alias, o.side, o.order_type, o.volume, o.activation_price, o.tp_price, o.sl_price, o.create_time
                    FROM mtapi_pending_orders o JOIN mtapi_connections c ON c.id = o.connection_id WHERE {_OWNED}
                    ORDER BY o.create_time DESC NULLS LAST, o.external_id
                """), params)).mappings().all()
                pending = [{
                    "id": r["external_id"], "symbol": r["symbol"], "alias": r["alias"], "side": r["side"], "type": r["order_type"],
                    "volume": _money(r["volume"]), "activation_price": _money(r["activation_price"]), "tp_price": _money(r["tp_price"]),
                    "sl_price": _money(r["sl_price"]), "create_time": _iso(r["create_time"]),
                } for r in rows]

            history = await self._history(conn, {**params, "_created": row["account_created_at"]}, freshness)
            request = (await conn.execute(text(f"""
                SELECT r.request_id, r.state, r.detail, r.resources, r.requested_at, r.started_at, r.finished_at
                FROM mtapi_sync_requests r JOIN mtapi_connections c ON c.id = r.connection_id WHERE {_OWNED}
            """), params)).mappings().first()
        return {
            "sync_request": public_sync_request(dict(request) if request else None),
            "connection": _connection_public(row, freshness), "summary": summary, "positions": positions, "pending_orders": pending,
            "freshness": freshness, "history": history, "server_time": _iso(now),
            "background_refresh": {"enabled": config.worker_enabled(), "live_interval_seconds": config.live_interval_seconds(),
                                   "history_interval_seconds": config.history_interval_seconds()},
            "service": config.service_status(user_id),
        }

    async def _history(self, conn, params: dict, freshness: dict) -> dict:
        """Coverage of imported closed positions plus an aggregate over the WHOLE imported set
        (computed in SQL, never from a page of rows)."""
        created = params.get("_created")
        params = {k: v for k, v in params.items() if not k.startswith("_")}
        cov = (await conn.execute(text(f"""
            SELECT COUNT(*) FILTER (WHERE i.status = 'queued' AND i.kind = 'backfill') AS queued,   -- the requested import; routine recent windows are always in flight
                   COUNT(*) FILTER (WHERE i.status = 'gap') AS gaps,
                   MIN(i.range_from) FILTER (WHERE i.kind = 'backfill') AS requested_from_ms,
                   MAX(i.range_to) FILTER (WHERE i.kind = 'backfill') AS requested_to_ms,
                   MIN(i.range_from) AS earliest_requested_ms
            FROM mtapi_history_intervals i JOIN mtapi_connections c ON c.id = i.connection_id
            WHERE {_OWNED} AND i.resource = 'closed_positions' AND i.status <> 'cancelled'
        """), params)).mappings().first()
        agg = (await conn.execute(text(f"""
            SELECT COUNT(*) AS entries, COUNT(DISTINCT t.position_id) AS positions,
                   COALESCE(SUM(t.net_profit), 0) AS net_profit,
                   COUNT(*) FILTER (WHERE t.net_profit > 0) AS winning, COUNT(*) FILTER (WHERE t.net_profit < 0) AS losing,
                   MIN(t.close_time) AS first_close, MAX(t.close_time) AS last_close
            FROM mtapi_closed_positions t JOIN mtapi_connections c ON c.id = t.connection_id WHERE {_OWNED}
        """), params)).mappings().first()
        if freshness["closed_positions"]["status"] == "not_permitted":
            status = "not_permitted"
        elif freshness["closed_positions"]["last_success_at"] is None:
            status = "pending"
        elif cov["earliest_requested_ms"] is None:
            status = "not_requested"          # rows exist but no import range was ever requested: nothing to call complete
        elif cov["gaps"]:
            status = "partial_gaps"
        elif cov["queued"]:
            status = "in_progress"
        else:
            status = "complete_for_requested_range"
        known = freshness["closed_positions"]["last_success_at"] is not None
        requested_from = cov["requested_from_ms"] if cov["requested_from_ms"] is not None else cov["earliest_requested_ms"]
        return {
            "coverage_status": status, "requested_from": _ms_iso(requested_from), "requested_to": _ms_iso(cov["requested_to_ms"]),
            "account_created_at": _iso(created) if created else None,
            "queued_intervals": int(cov["queued"]), "gap_count": int(cov["gaps"]),
            "closed_entry_count": int(agg["entries"]) if known else None,
            # Aggregate over everything imported so far. Entries are closing fills: a position closed
            # in parts counts once per part. Only meaningful as "the imported period" (see coverage_status).
            "period_stats": {
                "net_profit": _money(agg["net_profit"]), "closed_entries": int(agg["entries"]), "positions": int(agg["positions"]),
                "winning_entries": int(agg["winning"]), "losing_entries": int(agg["losing"]),
                "first_close_time": _iso(agg["first_close"]), "last_close_time": _iso(agg["last_close"]),
                "is_complete_for_requested_range": status == "complete_for_requested_range",
            } if known else None,
        }

    async def read_owned_analytics(self, connection_id: int, user_id: str, *, from_ms: int | None, to_ms: int | None, tz: str = "UTC") -> dict:
        """Realized performance of one connection over [from, to), computed in SQL over every stored
        closed entry in that range (not a page). Coverage is returned with it: figures can change
        while older history is still importing."""
        try:
            if not tz or len(tz) > 64:
                raise ZoneInfoNotFoundError(tz)
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise BadRange("Unknown time zone") from None
        if from_ms is not None and to_ms is not None and from_ms >= to_ms:
            raise BadRange("The start must be before the end")
        for value in (from_ms, to_ms):
            if value is not None and not (0 <= value <= 4_102_444_800_000):
                raise BadRange("Out-of-range timestamp")
        params: dict[str, Any] = {"id": connection_id, "user": user_id, "tz": tz,
                                  "from": datetime.fromtimestamp(from_ms / 1000, tz=timezone.utc) if from_ms is not None else None,
                                  "to": datetime.fromtimestamp(to_ms / 1000, tz=timezone.utc) if to_ms is not None else None}
        scope = f"""FROM mtapi_closed_positions t JOIN mtapi_connections c ON c.id = t.connection_id
                    WHERE {_OWNED} AND (CAST(:from AS timestamptz) IS NULL OR t.close_time >= CAST(:from AS timestamptz))
                      AND (CAST(:to AS timestamptz) IS NULL OR t.close_time < CAST(:to AS timestamptz))"""
        engine = self._engine_getter()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            row = (await conn.execute(text(f"""
                SELECT c.id, c.status, c.capabilities, c.account_currency, c.account_created_at FROM mtapi_connections c WHERE {_OWNED}
            """), params)).mappings().first()
            if not row:
                raise NotFound()
            now = (await conn.execute(text("SELECT NOW()"))).scalar()
            freshness = _freshness(await self._states(conn, connection_id, user_id), row["capabilities"] or {}, now)
            agg = (await conn.execute(text(f"""
                SELECT COUNT(*) AS entries, COUNT(DISTINCT t.position_id) AS positions, COALESCE(SUM(t.net_profit), 0) AS net,
                       COUNT(*) FILTER (WHERE t.net_profit > 0) AS wins, COUNT(*) FILTER (WHERE t.net_profit < 0) AS losses,
                       COUNT(*) FILTER (WHERE t.net_profit = 0) AS breakeven,
                       COALESCE(SUM(t.net_profit) FILTER (WHERE t.net_profit > 0), 0) AS gross_win,
                       COALESCE(SUM(t.net_profit) FILTER (WHERE t.net_profit < 0), 0) AS gross_loss,
                       COALESCE(SUM(t.commission), 0) AS commission, COALESCE(SUM(t.swap), 0) AS swap,
                       MIN(t.close_time) AS first_close, MAX(t.close_time) AS last_close
                {scope}
            """), params)).mappings().first()
            pos = (await conn.execute(text(f"""
                SELECT COUNT(*) FILTER (WHERE p.net > 0) AS wins, COUNT(*) FILTER (WHERE p.net < 0) AS losses, COUNT(*) FILTER (WHERE p.net = 0) AS breakeven
                FROM (SELECT t.position_id, SUM(t.net_profit) AS net {scope} GROUP BY t.position_id HAVING COUNT(t.net_profit) = COUNT(*)) p
            """), params)).mappings().first()
            daily = (await conn.execute(text(f"""
                SELECT (t.close_time AT TIME ZONE CAST(:tz AS text))::date AS day, COALESCE(SUM(t.net_profit), 0) AS net, COUNT(*) AS entries
                {scope} GROUP BY 1 ORDER BY 1 DESC LIMIT {MAX_ANALYTICS_DAYS}
            """), params)).mappings().all()
            best = (await conn.execute(text(f"""
                SELECT t.symbol, SUM(t.net_profit) AS net, COUNT(*) AS entries {scope} AND t.net_profit IS NOT NULL
                GROUP BY t.symbol ORDER BY SUM(t.net_profit) DESC, t.symbol LIMIT 1
            """), params)).mappings().first()
            history = await self._history(conn, {"id": connection_id, "user": user_id, "_created": row["account_created_at"]}, freshness)
        summary = analytics_summary(dict(agg), {k: int(v) for k, v in dict(pos).items()})
        days = list(reversed(daily))
        running = Decimal(0)
        cumulative = []
        for d in days:
            running += Decimal(d["net"])
            cumulative.append({"day": d["day"].isoformat(), "cumulative_net": _money(running)})
        # Positive-net days in a row, counting back from the most recent day with results.
        streak = 0
        for d in reversed(days):
            if Decimal(d["net"]) > 0:
                streak += 1
            else:
                break
        requested_from = history.get("requested_from")
        range_from_iso = _iso(params["from"]) if params["from"] else None
        return {
            "connection_id": connection_id, "currency": row["account_currency"], "time_zone": tz,
            "range": {"from": range_from_iso, "to": _iso(params["to"]) if params["to"] else None},
            "summary": summary,
            "best_symbol": {"symbol": best["symbol"], "net_profit": _money(best["net"]), "entries": int(best["entries"])} if best else None,
            "positive_day_streak": streak,
            "daily": [{"day": d["day"].isoformat(), "net_profit": _money(Decimal(d["net"])), "entries": int(d["entries"])} for d in days],
            "cumulative_realized": cumulative,
            "daily_truncated": len(daily) >= MAX_ANALYTICS_DAYS,
            "coverage": {
                "status": history["coverage_status"], "requested_from": requested_from, "requested_to": history["requested_to"],
                "queued_intervals": history["queued_intervals"], "gap_count": history["gap_count"],
                "account_created_at": history.get("account_created_at"),
                # The chosen range reaches further back than anything requested: older trades may simply not be imported.
                "range_starts_before_requested_history": bool(range_from_iso is None or (requested_from and range_from_iso < requested_from)) if requested_from else True,
                "may_change": history["coverage_status"] != "complete_for_requested_range",
            },
            "counting_rules": COUNTING_RULES,
            "not_derived": ["account equity history", "drawdown", "original risk", "MAE/MFE", "challenge or payout compliance"],
        }

    async def read_owned_progress(self, connection_id: int, user_id: str, *, tz: str, setup_enabled: bool) -> dict:
        """Daily P&L, the Challenge / payout headline and the progress rows (see progress.py),
        from one repeatable-read snapshot of the stored data and the saved setup profile."""
        from app.services.matchtrader_api import progress
        zone = progress.zone(tz)
        if zone is None:
            raise BadRange("Unknown time zone")
        engine = self._engine_getter()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            facts = await progress.gather(conn, self, connection_id, user_id, tz=zone, setup_enabled=setup_enabled)
        if facts is None:
            raise NotFound()
        return {"connection_id": connection_id, **progress.build(facts, tz)}

    async def read_owned_closed_trades(self, connection_id: int, user_id: str, *, limit: int, cursor: str | None) -> dict:
        """Keyset page over imported closed entries (newest first). This pages TaliTrade's database,
        not the broker."""
        limit = max(1, min(int(limit), MAX_TRADE_PAGE))
        params: dict[str, Any] = {"id": connection_id, "user": user_id, "n": limit + 1}
        keyset = ""
        engine = self._engine_getter()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            owned = (await conn.execute(text(f"SELECT 1 FROM mtapi_connections c WHERE {_OWNED}"), {"id": connection_id, "user": user_id})).first()
            if not owned:
                raise NotFound()   # decided before the cursor is even parsed: one answer for every non-owner request
            if cursor:
                params["ct"], params["cu"] = decode_cursor(cursor)
                keyset = "AND (t.close_time, t.uid) < (:ct, :cu)"
            rows = (await conn.execute(text(f"""
                SELECT t.uid, t.position_id, t.symbol, t.alias, t.side, t.volume, t.open_price, t.close_price, t.open_time, t.close_time,
                       t.profit, t.commission, t.swap, t.net_profit, t.close_reason
                FROM mtapi_closed_positions t JOIN mtapi_connections c ON c.id = t.connection_id
                WHERE {_OWNED} {keyset}
                ORDER BY t.close_time DESC, t.uid DESC LIMIT :n
            """), params)).mappings().all()
        page, more = rows[:limit], len(rows) > limit
        return {
            "trades": [{
                "entry_key": f"{account_key(connection_id)}:{r['uid']}", "uid": r["uid"], "position_id": r["position_id"],
                "symbol": r["symbol"], "alias": r["alias"], "side": r["side"], "volume": _money(r["volume"]),
                "open_price": _money(r["open_price"]), "close_price": _money(r["close_price"]),
                "open_time": _iso(r["open_time"]), "close_time": _iso(r["close_time"]),
                "profit": _money(r["profit"]), "commission": _money(r["commission"]), "swap": _money(r["swap"]),
                "net_profit": _money(r["net_profit"]), "close_reason": r["close_reason"],
            } for r in page],
            "next_cursor": encode_cursor(page[-1]["close_time"], page[-1]["uid"]) if more and page else None,
            "has_more": more,
        }


SYNC_REQUEST_PENDING = ("queued", "running")


def public_sync_request(row: dict | None) -> dict | None:
    """A "refresh now" request as shown to its owner. state: queued | running | completed |
    partial | failed | cancelled. Only 'completed' means every live resource was refreshed."""
    if not row:
        return None
    resources = row.get("resources") or {}
    if isinstance(resources, str):
        resources = json.loads(resources)
    return {"id": int(row["request_id"]), "state": row["state"], "detail": row.get("detail"), "resources": resources,
            "pending": row["state"] in SYNC_REQUEST_PENDING, "requested_at": _iso(row.get("requested_at")),
            "started_at": _iso(row.get("started_at")), "finished_at": _iso(row.get("finished_at"))}


def _ratio(numerator: Decimal, denominator: Decimal, places: str) -> str | None:
    if not denominator:
        return None
    return format((numerator / denominator).quantize(Decimal(places), rounding=ROUND_HALF_EVEN), "f")


def _percent(part: int, whole: int) -> str | None:
    return _ratio(Decimal(part) * 100, Decimal(whole), "0.1") if whole else None


def analytics_summary(row: dict, positions: dict) -> dict:
    """Turn the SQL aggregate into reported figures. Pure: easy to check by hand.

    Counting rules (also returned with the figures):
    * an *entry* is one closing fill (uid). A position closed in parts counts once per part;
    * a *position* is grouped by position id, with its entries' net results added up;
    * net results are the broker's ``netProfit`` (commission and swap already included); they are
      never added again. Cash ledger rows (deposits, withdrawals, CLOSE_POSITION) are not counted;
    * breakeven = exactly zero net. Win rate = winning / all entries with a net result, so
      breakevens count as not winning. Entries without a net result are reported, not counted;
    * profit factor = gross wins / |gross losses|; with no losses it is undefined, not infinite."""
    entries, wins, losses, breakeven = int(row["entries"]), int(row["wins"]), int(row["losses"]), int(row["breakeven"])
    counted = wins + losses + breakeven
    gross_win, gross_loss = Decimal(row["gross_win"] or 0), Decimal(row["gross_loss"] or 0)
    if gross_loss:
        pf, pf_note = _ratio(gross_win, -gross_loss, "0.01"), None
    else:
        pf, pf_note = None, ("no_losses" if gross_win else "no_results")
    return {
        "net_profit": _money(Decimal(row["net"] or 0)), "closed_entries": entries, "entries_with_result": counted,
        "entries_without_result": entries - counted, "positions": int(row["positions"]),
        "winning_entries": wins, "losing_entries": losses, "breakeven_entries": breakeven,
        "entry_win_rate_percent": _percent(wins, counted),
        "winning_positions": positions["wins"], "losing_positions": positions["losses"], "breakeven_positions": positions["breakeven"],
        "position_win_rate_percent": _percent(positions["wins"], positions["wins"] + positions["losses"] + positions["breakeven"]),
        "gross_profit": _money(gross_win), "gross_loss": _money(gross_loss),
        "average_win": _ratio(gross_win, Decimal(wins), "0.01") if wins else None,
        "average_loss": _ratio(gross_loss, Decimal(losses), "0.01") if losses else None,
        "profit_factor": pf, "profit_factor_undefined_reason": pf_note,
        "commission_total": _money(Decimal(row["commission"] or 0)), "swap_total": _money(Decimal(row["swap"] or 0)),
        "first_close_time": _iso(row["first_close"]), "last_close_time": _iso(row["last_close"]),
    }


COUNTING_RULES = {
    "entry": "One closing fill (uid). A position closed in parts counts once per part.",
    "position": "Entries grouped by position id, net results added up.",
    "net_profit": "The broker's net result per entry; commission and swap are already included and shown separately for information only.",
    "win_rate": "Winning entries divided by entries with a net result. Breakeven (exactly zero) counts as not winning.",
    "profit_factor": "Gross wins divided by the absolute gross losses. Undefined when there are no losses.",
    "averages": "Rounded to 2 decimal places. Profit factor to 2, percentages to 1.",
    "excluded": "Cash ledger entries (deposits, withdrawals, CLOSE_POSITION adjustments) are not counted as trade results.",
    "daily": "Grouped by the calendar day of the close time in the stated time zone.",
    "cumulative": "Running total of realized net results by day. It is not an account-equity or balance curve.",
}


def interpret_sync_outcome(outcome: dict) -> dict:
    """Turn engine.sync_connection()'s per-resource outcome into one honest verdict.

    HTTP 200 from the sync route only means "the request ran". ``state`` says what happened:
    ok | partial | busy | needs_reauth | rate_limited | failed | not_active.
    ``live_ready`` is true only when summary AND positions were persisted in this pass.
    """
    skipped = outcome.get("skipped")
    if skipped:
        state = {"busy": "busy", "needs_reauth": "needs_reauth", "credential_changed": "busy"}.get(skipped, "not_active")
        return {"state": state, "live_ready": False, "resources": {}, "detail": skipped}
    resources = {name: value for name, value in outcome.items() if isinstance(value, str)}
    if "unauthorized" in resources.values():
        return {"state": "needs_reauth", "live_ready": False, "resources": resources, "detail": "unauthorized"}
    live_ready = resources.get("summary") == "ok" and resources.get("positions") == "ok"
    problems = {name: value for name, value in resources.items() if value not in ("ok", "not_permitted")}
    if not problems:
        state = "ok"
    elif live_ready:
        state = "partial"
    elif any(value == "rate_limited" for value in (resources.get("summary"), resources.get("positions"))):
        state = "rate_limited"
    elif "fenced" in problems.values():
        state = "busy"
    else:
        state = "failed"
    return {"state": state, "live_ready": live_ready, "resources": resources, "detail": None}
