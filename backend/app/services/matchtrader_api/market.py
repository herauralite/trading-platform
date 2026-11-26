"""Market data for the dashboard ticker and price chart: instruments, bid/ask snapshots, candles.

How it is kept safe and cheap
-----------------------------
* The broker is only ever called by the worker, with the connection's own encrypted token.
  Browser requests read the cache in PostgreSQL and never contact the broker.
* The cache is per connection (one verified account on one platform). Nothing is shared between
  users or between broker environments.
* Polling follows interest: a dashboard read marks "someone is looking" for a short time
  (config.market_interest_seconds). When nobody reads, polling stops. Ten open tabs mark the
  same row, so they do not multiply broker calls. Account sync does not depend on any of this.
* Market calls are low priority in the shared rate budget: account sync is served first, and a
  market call that would exceed the budget is skipped and retried later.
* A market pass takes the same single-writer lease as account sync and costs two database
  statements: take the lease; store everything + record state + release.

What the data means
-------------------
* A quote is a snapshot with the PROVIDER's timestamp. A successful fetch does not mean the
  price just changed: with the session closed it can be hours old and still correct.
* A candle's timestamp is its opening time. The newest candle is still forming and is
  overwritten on each refresh. ``tick_count`` counts price ticks; it is not traded volume.
* The provider does not say which price side candles are built from, so none is claimed.

Live instruments (US30 only, for now; config.market_live_enabled)
-----------------------------------------------------------------
* The Trader API used here is REST only: no streaming endpoint is in the client's allowlist or
  verified for these tokens, so "live" means a faster, shared quote poll, not a stream.
* The broker's symbol is resolved from the account's own instrument list (US30, DJI30 as
  MetaTrader names it, and other common names). Missing or ambiguous is reported, never guessed.
* A 'live:<instrument>' feed row per connection drives one batched /v1/quotes request every
  config.market_live_quote_interval_seconds(), however many tabs are open. It runs at the
  budget's "live" priority: above backfill, below account sync.
* Candles stay the source of history. A quote may only move the forming candle once the candle
  price side is VERIFIED: when a forming candle and a quote arrive in the same pass, the candle's
  close is compared with bid, ask and mid; the tallies are kept in mtapi_market_price_basis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.services.matchtrader_api import client as api, config, crypto, ratelimit
from app.services.matchtrader_api.store import _GUARD, SqlStore, _rows_json

logger = logging.getLogger("app.matchtrader_api")

INTERVAL_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800, "MN1": 2592000}
UI_INTERVALS = ("M1", "M5", "H1")           # offered in the dashboard to begin with
DEFAULT_INTERVAL = "M5"
WATCHLIST_SIZE = 6
MAX_CHART_FEEDS = 3                          # candle feeds kept alive per connection (several tabs / intervals)
FORBIDDEN_RETRY_SECONDS = 3600
# Instruments looked for first, by normalized name. Only ones the account really offers are used.
WATCHLIST_PREFERENCES = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "US30", "NAS100", "SPX500")
CHART_PREFERENCE = "EURUSD"
# Instruments that may be charted live: TaliTrade's name -> names brokers list it under, in order of
# preference. DJI30 is MetaTrader's name for the same Dow Jones 30 index CFD.
LIVE_INSTRUMENTS = {"US30": ("US30", "DJI30", "DJ30", "WS30", "USA30", "DOW30", "US30CASH", "DJIA")}
LIVE_PREFIX = "live:"
BASIS_MIN_SAMPLES = 3            # matching samples needed before quotes may move a candle
BASIS_AGREEMENT = 0.9            # ...and the share of conclusive samples that must agree

DDL = [
    """CREATE TABLE IF NOT EXISTS mtapi_market_symbols (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL, alias TEXT, description TEXT, type TEXT, base_currency TEXT, quote_currency TEXT,
        price_precision INTEGER, session_open BOOLEAN, fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_market_quotes (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL, alias TEXT, bid NUMERIC, ask NUMERIC, provider_time TIMESTAMPTZ, fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS mtapi_market_candles (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL, interval TEXT NOT NULL, open_time TIMESTAMPTZ NOT NULL,
        open NUMERIC NOT NULL, high NUMERIC NOT NULL, low NUMERIC NOT NULL, close NUMERIC NOT NULL, tick_count INTEGER,
        fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, symbol, interval, open_time)
    )""",
    # One row per thing that can be polled for a connection: 'symbols', 'quotes', 'candles:<symbol>:<interval>'.
    # interest_until says whether anyone is looking; next_due_at when it should be fetched next.
    """CREATE TABLE IF NOT EXISTS mtapi_market_feeds (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        key TEXT NOT NULL, symbol TEXT, interval TEXT, watchlist JSONB,
        interest_until TIMESTAMPTZ NOT NULL, next_due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_attempt_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ, last_error_code TEXT,
        PRIMARY KEY (connection_id, key)
    )""",
    # Evidence for which price a symbol's candles are built from (see basis_sample).
    """CREATE TABLE IF NOT EXISTS mtapi_market_price_basis (
        connection_id INTEGER NOT NULL REFERENCES mtapi_connections(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL, bid_hits INTEGER NOT NULL DEFAULT 0, ask_hits INTEGER NOT NULL DEFAULT 0,
        mid_hits INTEGER NOT NULL DEFAULT 0, misses INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (connection_id, symbol)
    )""",
]
TABLES = ["mtapi_market_feeds", "mtapi_market_candles", "mtapi_market_quotes", "mtapi_market_symbols", "mtapi_market_price_basis"]


class NotFound(Exception):
    """Unknown, other-owned or disconnected connection."""


class UnknownSymbol(Exception):
    """The account does not offer this instrument."""


# ── pure helpers ─────────────────────────────────────────────────────────────
def normalize(name: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def _matches(symbol: dict, wanted: str) -> bool:
    """Exact normalized match on the provider symbol or its alias, allowing a broker suffix
    (EURUSD.x, EURUSDm). Never a substring match in the middle of a name."""
    for name in (normalize(symbol.get("symbol")), normalize(symbol.get("alias"))):
        if name == wanted or (name.startswith(wanted) and len(name) - len(wanted) <= 2):
            return True
    return False


def is_fx_pair(symbol: dict) -> bool:
    base, quote = str(symbol.get("base_currency") or ""), str(symbol.get("quote_currency") or "")
    return str(symbol.get("type") or "").upper() == "FOREX" and len(base) == 3 and len(quote) == 3


def choose_watchlist(symbols: list[dict], size: int = WATCHLIST_SIZE) -> list[str]:
    """Provider symbols for the ticker, taken ONLY from what the account offers: preferred
    instruments that exist, then the first FX pairs, in the provider's own order."""
    chosen: list[str] = []
    for wanted in WATCHLIST_PREFERENCES:
        hit = next((s for s in symbols if _matches(s, wanted)), None)
        if hit and hit["symbol"] not in chosen:
            chosen.append(hit["symbol"])
    for s in symbols:
        if len(chosen) >= size:
            break
        if is_fx_pair(s) and s["symbol"] not in chosen:
            chosen.append(s["symbol"])
    return chosen[:size]


def choose_chart_symbol(symbols: list[dict]) -> dict | None:
    """EURUSD if the account offers it, otherwise its first FX pair, otherwise nothing."""
    return next((s for s in symbols if _matches(s, CHART_PREFERENCE)), None) or next((s for s in symbols if is_fx_pair(s)), None)


def resolve_instrument(symbols: list[dict], instrument: str) -> dict:
    """Which of the account's own symbols is ``instrument`` (e.g. US30)?

    status: 'resolved' (exactly one), 'ambiguous' (several equally good: nothing is picked) or
    'missing'. An exact name (symbol or alias, ignoring punctuation and case) beats a name with a
    short broker suffix (US30.x, US30m); never a substring in the middle of a name."""
    names = LIVE_INSTRUMENTS[instrument]

    def exact(s: dict) -> bool:
        return any(normalize(s.get(field)) == name for field in ("symbol", "alias") for name in names)

    def suffixed(s: dict) -> bool:      # US30m, US30.f, US30_cash: letters only, so US300 or US3000 never pass for US30
        for field in ("symbol", "alias"):
            value = normalize(s.get(field))
            if any(value.startswith(n) and 1 <= len(value) - len(n) <= 4 and value[len(n):].isalpha() for n in names):
                return True
        return False

    unique = list({s["symbol"]: s for s in symbols if s.get("symbol")}.values())
    for tier in ([s for s in unique if exact(s)], [s for s in unique if not exact(s) and suffixed(s)]):
        if len(tier) == 1:
            return {"instrument": instrument, "status": "resolved", "symbol": tier[0], "candidates": []}
        if len(tier) > 1:
            return {"instrument": instrument, "status": "ambiguous", "symbol": None,
                    "candidates": [{"symbol": s["symbol"], "alias": s.get("alias"), "description": s.get("description")} for s in tier]}
    return {"instrument": instrument, "status": "missing", "symbol": None, "candidates": []}


def basis_sample(close: Decimal | None, bid: Decimal | None, ask: Decimal | None) -> str:
    """Compare a forming candle's close with a quote taken in the same pass: 'bid', 'ask' or
    'mid' when exactly one of them equals the close, otherwise 'miss' (the price moved between
    the two requests, the spread is zero, or something is missing: not evidence either way)."""
    if close is None or bid is None or ask is None or bid == ask:
        return "miss"
    hits = [name for name, value in (("bid", bid), ("ask", ask), ("mid", (bid + ask) / 2)) if value == close]
    return hits[0] if len(hits) == 1 else "miss"


def basis_verdict(counts: dict | None) -> str:
    """'bid' | 'ask' | 'mid' once enough samples agree, else 'unverified'."""
    counts = {k: int((counts or {}).get(f"{k}_hits") or 0) for k in ("bid", "ask", "mid")}
    conclusive = sum(counts.values())
    side, hits = max(counts.items(), key=lambda kv: kv[1])
    return side if hits >= BASIS_MIN_SAMPLES and hits >= BASIS_AGREEMENT * conclusive else "unverified"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _price(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def feed_status(feed: dict | None, *, has_data: bool, now: datetime, poll_seconds: int, session_open: bool | None = None,
                stale_after: float | None = None) -> tuple[str, str | None]:
    """(status, detail). status: loading | fresh | market_closed | stale | permission_denied | unavailable."""
    error = feed.get("last_error_code") if feed else None
    if error == "deferred":
        error = None               # waiting for budget is not a failure: report the data as it is
    if error == "forbidden":
        return "permission_denied", "This API token is not allowed to read market data. Ask your broker to enable market data for Trader API, or create a token that includes it."
    if error == "symbol_not_found":
        return "unavailable", "The broker no longer offers this instrument."
    if not has_data:
        if error:
            return "unavailable", {"rate_limited": "The broker's request limit was reached. Prices will be retried shortly.",
                                   "needs_reauth": "The broker rejected the saved token. Reconnect the account."}.get(error, "The broker could not be reached for market data. It will be retried.")
        return "loading", None
    if session_open is False:
        return "market_closed", "Market closed: these are the last prices the broker published."
    last = feed.get("last_success_at") if feed else None
    if last is None or (now - last).total_seconds() > (stale_after if stale_after is not None else max(3 * poll_seconds, 60)):
        return "stale", "Prices have not refreshed recently."
    return "fresh", None


# ── persistence (single statements) ──────────────────────────────────────────
async def ensure_schema(store: SqlStore) -> None:
    async with store.engine.begin() as conn:
        from sqlalchemy import text
        for statement in DDL:
            await conn.execute(text(statement))


async def due(store: SqlStore) -> dict[int, list[dict]]:
    """Feeds to fetch now, grouped by connection: someone is looking, it is due, the connection
    is active and nobody holds its lease."""
    rows = await store._one("""
        SELECT f.connection_id, f.key, f.symbol, f.interval, f.watchlist, f.last_success_at,
               EXTRACT(EPOCH FROM NOW() - f.last_success_at) AS success_age,
               -- context for sharing one quote request: instruments charted live right now, and the ticker's list
               (SELECT jsonb_agg(l.symbol) FROM mtapi_market_feeds l WHERE l.connection_id = f.connection_id AND l.key LIKE 'live:%'
                  AND l.interest_until > NOW() AND l.symbol IS NOT NULL) AS live_symbols,
               (SELECT t.watchlist FROM mtapi_market_feeds t WHERE t.connection_id = f.connection_id AND t.key = 'quotes'
                  AND t.interest_until > NOW()) AS ticker_watchlist
        FROM mtapi_market_feeds f JOIN mtapi_connections c ON c.id = f.connection_id
        WHERE c.status = 'active' AND f.interest_until > NOW() AND f.next_due_at <= NOW()
          AND (c.lease_owner IS NULL OR c.lease_expires_at < NOW())
        ORDER BY f.connection_id, f.key DESC
    """, {})
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["connection_id"], []).append(row)
    return grouped


async def seconds_until_next_due(store: SqlStore) -> float | None:
    rows = await store._one("""
        SELECT EXTRACT(EPOCH FROM MIN(f.next_due_at) - NOW()) AS seconds
        FROM mtapi_market_feeds f JOIN mtapi_connections c ON c.id = f.connection_id
        WHERE c.status = 'active' AND f.interest_until > NOW()
    """, {})
    value = rows[0]["seconds"] if rows else None
    return float(value) if value is not None else None


async def save(store: SqlStore, connection_id: int, generation: int, owner: str, *, symbols: list[dict] | None, quotes: list[dict] | None,
               candles: list[dict], feeds: list[dict], watchlist: list[str] | None, quote_scope: list[str] | None = None,
               basis: list[dict] | None = None) -> bool:
    """Store one market pass and release the lease, in one statement. ``feeds``: {key, ok, err, due_in, drop}.
    ``quote_scope``: the symbols the quotes answer was asked for (only those can be "left out").
    ``basis``: price-basis samples, {symbol, side} with side in bid | ask | mid | miss."""
    if quotes is not None and quote_scope is None:
        quote_scope = [q["symbol"] for q in quotes]
    rows = await store._one(f"""
        WITH {_GUARD},
        sym_put AS (
            INSERT INTO mtapi_market_symbols (connection_id, symbol, alias, description, type, base_currency, quote_currency, price_precision, session_open, fetched_at)
            SELECT CAST(:id AS integer), x.symbol, x.alias, x.description, x.type, x.base_currency, x.quote_currency, x.price_precision, x.session_open, NOW()
            FROM jsonb_to_recordset(CAST(:symbols AS jsonb)) AS x(symbol text, alias text, description text, type text, base_currency text,
                                                                  quote_currency text, price_precision integer, session_open boolean)
            WHERE EXISTS (SELECT 1 FROM g)
            ON CONFLICT (connection_id, symbol) DO UPDATE SET alias = EXCLUDED.alias, description = EXCLUDED.description, type = EXCLUDED.type,
                base_currency = EXCLUDED.base_currency, quote_currency = EXCLUDED.quote_currency, price_precision = EXCLUDED.price_precision,
                session_open = EXCLUDED.session_open, fetched_at = EXCLUDED.fetched_at
            RETURNING 1),
        sym_del AS (   -- only a successful full list may remove instruments the account no longer offers
            DELETE FROM mtapi_market_symbols s WHERE s.connection_id = :id AND CAST(:has_symbols AS boolean) AND EXISTS (SELECT 1 FROM g)
              AND s.symbol NOT IN (SELECT x.symbol FROM jsonb_to_recordset(CAST(:symbols AS jsonb)) AS x(symbol text))
            RETURNING 1),
        q_put AS (
            INSERT INTO mtapi_market_quotes (connection_id, symbol, alias, bid, ask, provider_time, fetched_at)
            SELECT CAST(:id AS integer), x.symbol, x.alias, x.bid, x.ask, x.provider_time, NOW()
            FROM jsonb_to_recordset(CAST(:quotes AS jsonb)) AS x(symbol text, alias text, bid numeric, ask numeric, provider_time timestamptz)
            WHERE EXISTS (SELECT 1 FROM g)
            ON CONFLICT (connection_id, symbol) DO UPDATE SET alias = EXCLUDED.alias, bid = EXCLUDED.bid, ask = EXCLUDED.ask,
                provider_time = EXCLUDED.provider_time, fetched_at = EXCLUDED.fetched_at
            -- never step back in time: an older or out-of-order snapshot leaves the newer one in place
            WHERE EXCLUDED.provider_time IS NULL OR mtapi_market_quotes.provider_time IS NULL OR EXCLUDED.provider_time >= mtapi_market_quotes.provider_time
            RETURNING 1),
        q_del AS (     -- instruments the answer was asked for but left out have no current quote
            DELETE FROM mtapi_market_quotes q WHERE q.connection_id = :id AND CAST(:has_quotes AS boolean) AND EXISTS (SELECT 1 FROM g)
              AND q.symbol IN (SELECT jsonb_array_elements_text(CAST(:quote_scope AS jsonb)))
              AND q.symbol NOT IN (SELECT x.symbol FROM jsonb_to_recordset(CAST(:quotes AS jsonb)) AS x(symbol text))
            RETURNING 1),
        b_put AS (     -- price-basis evidence accumulates; one row per symbol
            INSERT INTO mtapi_market_price_basis AS b (connection_id, symbol, bid_hits, ask_hits, mid_hits, misses, updated_at)
            SELECT CAST(:id AS integer), x.symbol, COUNT(*) FILTER (WHERE x.side = 'bid'), COUNT(*) FILTER (WHERE x.side = 'ask'),
                   COUNT(*) FILTER (WHERE x.side = 'mid'), COUNT(*) FILTER (WHERE x.side = 'miss'), NOW()
            FROM jsonb_to_recordset(CAST(:basis AS jsonb)) AS x(symbol text, side text)
            WHERE EXISTS (SELECT 1 FROM g)
            GROUP BY x.symbol
            ON CONFLICT (connection_id, symbol) DO UPDATE SET bid_hits = b.bid_hits + EXCLUDED.bid_hits, ask_hits = b.ask_hits + EXCLUDED.ask_hits,
                mid_hits = b.mid_hits + EXCLUDED.mid_hits, misses = b.misses + EXCLUDED.misses, updated_at = EXCLUDED.updated_at
            RETURNING 1),
        c_put AS (     -- keyed by opening time: the forming candle is overwritten, never appended twice
            INSERT INTO mtapi_market_candles (connection_id, symbol, interval, open_time, open, high, low, close, tick_count, fetched_at)
            SELECT CAST(:id AS integer), x.symbol, x.interval, x.open_time, x.open, x.high, x.low, x.close, x.tick_count, NOW()
            FROM jsonb_to_recordset(CAST(:candles AS jsonb)) AS x(symbol text, interval text, open_time timestamptz, open numeric, high numeric,
                                                                  low numeric, close numeric, tick_count integer)
            WHERE EXISTS (SELECT 1 FROM g)
            ON CONFLICT (connection_id, symbol, interval, open_time) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, tick_count = EXCLUDED.tick_count, fetched_at = EXCLUDED.fetched_at
            RETURNING 1),
        feed AS (
            UPDATE mtapi_market_feeds f SET
                last_attempt_at = NOW(),
                last_success_at = CASE WHEN x.ok THEN NOW() ELSE f.last_success_at END,
                last_error_code = x.err,
                next_due_at = NOW() + make_interval(secs => x.due_in),
                interest_until = CASE WHEN x.drop THEN NOW() ELSE f.interest_until END,
                watchlist = CASE WHEN f.key = 'quotes' AND CAST(:watchlist AS jsonb) IS NOT NULL THEN CAST(:watchlist AS jsonb) ELSE f.watchlist END
            FROM jsonb_to_recordset(CAST(:feeds AS jsonb)) AS x(key text, ok boolean, err text, due_in double precision, "drop" boolean)
            WHERE f.connection_id = :id AND f.key = x.key AND EXISTS (SELECT 1 FROM g)
            RETURNING 1),
        watch AS (     -- a fresh instrument list updates the ticker's watchlist even when quotes were not due
            UPDATE mtapi_market_feeds f SET watchlist = CAST(:watchlist AS jsonb)
            WHERE f.connection_id = :id AND f.key = 'quotes' AND CAST(:watchlist AS jsonb) IS NOT NULL AND EXISTS (SELECT 1 FROM g)
              AND NOT EXISTS (SELECT 1 FROM jsonb_to_recordset(CAST(:feeds AS jsonb)) AS x(key text) WHERE x.key = 'quotes')
            RETURNING 1),
        lease AS (
            UPDATE mtapi_connections c SET lease_owner = NULL, lease_expires_at = NULL FROM g WHERE c.id = g.id RETURNING 1)
        SELECT EXISTS (SELECT 1 FROM g) AS ok
    """, {"id": connection_id, "gen": generation, "o": owner,
          "symbols": _rows_json(symbols or []), "has_symbols": symbols is not None,
          "quotes": _rows_json(quotes or []), "has_quotes": quotes is not None, "quote_scope": json.dumps(quote_scope or []),
          "basis": json.dumps(basis or []),
          "candles": _rows_json(candles), "feeds": json.dumps(feeds), "watchlist": json.dumps(watchlist) if watchlist is not None else None})
    return bool(rows and rows[0]["ok"])


# ── acquisition (worker only) ────────────────────────────────────────────────
def _count_paths(client) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path, _seconds in getattr(client, "timings", []) or []:
        name = path.rsplit("/", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    return counts


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, api.Unauthorized):
        return "unauthorized"
    if isinstance(exc, api.Forbidden):
        return "forbidden"
    if isinstance(exc, ratelimit.BudgetExhausted):
        return "deferred"          # TaliTrade's own budget held it back (account sync comes first); the broker was not asked
    if isinstance(exc, api.RateLimited):
        return "rate_limited"      # the broker answered 429
    if isinstance(exc, api.RequestRejected) and getattr(exc, "status", None) == 404:
        return "symbol_not_found"
    if isinstance(exc, api.TransientError):
        return "transient"
    if isinstance(exc, api.ContractError):
        return "contract"
    if isinstance(exc, api.MatchTraderApiError):
        return "rejected"
    return "internal"


def _retry_in(code: str, exc: BaseException, normal: int) -> float:
    if code == "forbidden":
        return FORBIDDEN_RETRY_SECONDS            # a permission does not appear by retrying every few seconds
    if code == "deferred":
        return max(float(getattr(exc, "retry_in", 0) or 0) + 1.0, 5.0)   # as soon as the budget has room again
    if code == "rate_limited":
        wait = getattr(exc, "retry_in", None) or getattr(exc, "retry_after", None) or 30
        return max(float(wait) + 1.0, float(normal))
    return float(max(normal, 30)) if code != "internal" else 120.0


async def run_pass(store: SqlStore, connection_id: int, feeds: list[dict], *, owner: str, client_factory) -> dict:
    """Fetch the due market feeds of one connection and store them. Never raises for broker
    errors: each feed records its own state, and account sync is unaffected."""
    started = time.monotonic()
    upstream: dict[str, int] = {}
    async with store.session() as session:
        opened = await store.open_pass(connection_id, owner, config.LEASE_SECONDS, claim_request=False)
        if opened is None:
            return {"skipped": "busy"}
        generation = int(opened["generation"])
        results: dict[str, str] = {}
        try:
            if not opened.get("ciphertext") or opened.get("credential_generation") != generation:
                return {"skipped": "credential_changed"}
            try:
                token = crypto.decrypt_token(opened["ciphertext"], crypto.build_associated_data(
                    user_id=opened["user_id"], connection_id=connection_id,
                    platform_host=opened["platform_host"], external_login=opened["external_login"]))
            except crypto.TokenCryptoError:
                return {"skipped": "credential_unreadable"}   # account sync reports this state; market data just stops
            budget = ratelimit.budget_for(f"{opened['platform_host']}|{opened['external_login']}")
            by_key = {f["key"]: f for f in feeds}
            symbols_rows: list[dict] | None = None
            quotes_rows: list[dict] | None = None
            candle_rows: list[dict] = []
            outcome: list[dict] = []
            watchlist: list[str] | None = None
            quote_scope: list[str] = []
            forming: dict[str, Any] = {}
            now_ms = int(time.time() * 1000)

            def record(key: str, exc: BaseException | None, normal: int) -> None:
                if exc is None:
                    results[key] = "ok"
                    outcome.append({"key": key, "ok": True, "err": None, "due_in": float(normal), "drop": False})
                    return
                code = _error_code(exc)
                results[key] = code
                if code == "internal":
                    logger.error("matchtrader market step failed connection=%s feed=%s error_type=%s", connection_id, key.split(":")[0], type(exc).__name__)
                outcome.append({"key": key, "ok": False, "err": code, "due_in": _retry_in(code, exc, normal), "drop": code == "symbol_not_found"})

            async with client_factory(token, budget) as client:
                del token
                # 1) instruments first: the watchlist and session state come from them
                if "symbols" in by_key:
                    try:
                        listed = await client.symbols()
                        symbols_rows = [{"symbol": s.symbol, "alias": s.alias, "description": s.description, "type": s.type,
                                         "base_currency": s.baseCurrency, "quote_currency": s.quoteCurrency,
                                         "price_precision": s.pricePrecision, "session_open": s.sessionOpen} for s in listed]
                        symbols_rows = list({row["symbol"]: row for row in symbols_rows}.values())
                        watchlist = choose_watchlist(symbols_rows)
                        record("symbols", None, config.market_symbols_interval_seconds())
                    except Exception as exc:
                        record("symbols", exc, config.market_symbols_interval_seconds())
                # 2) quotes (ONE batched request shared by the ticker and every live chart) and candles, side by side
                jobs: list[tuple[str, Any, int, dict | None]] = []
                context = feeds[0] if feeds else {}
                live_now = set(context.get("live_symbols") or []) if config.market_live_enabled() else set()
                live_due = [k for k in by_key if k.startswith(LIVE_PREFIX)] if config.market_live_enabled() else []
                if not config.market_live_enabled():      # switched off: stop polling those feeds instead of finding them due forever
                    outcome.extend({"key": k, "ok": False, "err": None, "due_in": 60.0, "drop": True} for k in by_key if k.startswith(LIVE_PREFIX))
                # A live symbol's candle refresh takes a quote in the same pass: that is how the candle price side is verified.
                basis_for = {f["symbol"] for k, f in by_key.items() if k.startswith("candles:") and f["symbol"] in live_now}
                ticker_list = watchlist if watchlist is not None else list((by_key.get("quotes") or {}).get("watchlist") or context.get("ticker_watchlist") or [])
                quote_keys: list[str] = []
                wanted: list[str] = []
                if "quotes" in by_key or (live_due and ticker_list):   # a live request carries the ticker along for free
                    if ticker_list:
                        wanted += ticker_list
                        quote_keys.append("quotes")
                    elif "symbols" not in results or results["symbols"] == "ok":
                        # no instrument list yet (or nothing suitable): come back when there is one
                        outcome.append({"key": "quotes", "ok": False, "err": by_key["quotes"].get("last_error_code") if watchlist is None else None,
                                        "due_in": 5.0 if watchlist is None else float(config.market_symbols_interval_seconds()), "drop": False})
                for key in live_due:
                    quote_keys.append(key)
                    wanted.append(by_key[key]["symbol"])
                wanted += sorted(basis_for)
                wanted = list(dict.fromkeys(s for s in wanted if s))
                live_priority = bool(live_due or basis_for)
                quote_scope = wanted
                if wanted:
                    jobs.append(("quotes", client.quotes(wanted, live=live_priority), config.market_quote_interval_seconds(), None))
                for key, feed in by_key.items():
                    if not key.startswith("candles:"):
                        continue
                    interval = feed["interval"]
                    age = feed.get("success_age")
                    # A short gap only needs the forming candle and its neighbours; anything longer reloads the window.
                    recent = age is not None and float(age) <= 2 * INTERVAL_SECONDS.get(interval, 60)
                    limit = 3 if recent else config.market_candle_window()
                    jobs.append((key, client.candles(feed["symbol"], interval, range_to=now_ms, limit=limit, live=feed["symbol"] in live_now),
                                 config.market_candle_interval_seconds(), feed))
                answers = await asyncio.gather(*(job[1] for job in jobs), return_exceptions=True)
                upstream.update(_count_paths(client))
                for (key, _, normal, feed), answer in zip(jobs, answers):
                    if isinstance(answer, BaseException):
                        if not isinstance(answer, Exception):
                            raise answer
                        if key == "quotes":
                            for k in quote_keys:
                                record(k, answer, config.market_live_quote_interval_seconds() if k.startswith(LIVE_PREFIX) else normal)
                        else:
                            record(key, answer, normal)
                    elif key == "quotes":
                        quotes_rows = list({q.symbol: {"symbol": q.symbol, "alias": q.alias, "bid": q.bid, "ask": q.ask,
                                                       "provider_time": datetime.fromtimestamp(q.timestamp / 1000, tz=timezone.utc) if q.timestamp else None}
                                            for q in answer}.values())
                        for k in quote_keys:
                            record(k, None, config.market_live_quote_interval_seconds() if k.startswith(LIVE_PREFIX) else normal)
                    else:
                        if answer and feed["symbol"] in basis_for:
                            newest = max(answer, key=lambda c: c.timestamp)
                            if newest.timestamp + INTERVAL_SECONDS.get(feed["interval"], 60) * 1000 > now_ms:   # still forming
                                forming[feed["symbol"]] = newest
                        merged = {c.timestamp: c for c in answer}         # duplicates by opening time: the last one wins
                        candle_rows.extend({"symbol": feed["symbol"], "interval": feed["interval"],
                                            "open_time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc), "open": c.open, "high": c.high,
                                            "low": c.low, "close": c.close, "tick_count": c.tickCount} for ts, c in sorted(merged.items()))
                        record(key, None, normal)
            if "unauthorized" in results.values():
                # The token itself is no longer accepted. Same handling as account sync: ask for a new one.
                await store.set_status(connection_id, "needs_reauth", "unauthorized", only_from=("active",))
                return results
            basis_rows = []
            if quotes_rows is not None:
                by_symbol = {row["symbol"]: row for row in quotes_rows}
                for symbol, candle in forming.items():
                    quote = by_symbol.get(symbol)
                    if quote is not None:
                        basis_rows.append({"symbol": symbol, "side": basis_sample(candle.close, quote["bid"], quote["ask"])})
            if not await save(store, connection_id, generation, owner, symbols=symbols_rows, quotes=quotes_rows, candles=candle_rows,
                              feeds=outcome, watchlist=watchlist, quote_scope=quote_scope if quotes_rows is not None else None, basis=basis_rows):
                return {"skipped": "fenced"}
            opened = None                                         # the save released the lease
            return results
        finally:
            if opened is not None:
                await store.release_lease(connection_id, owner)
            # upstream=: broker requests this pass by endpoint (no symbols, no prices): request volume can be read from logs.
            logger.info("matchtrader market pass connection=%s seconds=%s feeds=%s db_calls=%s upstream=%s", connection_id,
                        round(time.monotonic() - started, 3), len(feeds), session.db_calls,
                        ",".join(f"{k}:{v}" for k, v in sorted(upstream.items())) or "none")


async def run_due_once(store: SqlStore, *, client_factory, owner: str) -> int:
    """One market round. Called by the worker AFTER account passes, only when the feature is on."""
    handled = 0
    for connection_id, feeds in (await due(store)).items():
        try:
            await run_pass(store, connection_id, feeds, owner=owner, client_factory=client_factory)
            handled += 1
        except Exception as exc:
            logger.error("matchtrader market pass failed connection=%s error_type=%s", connection_id, type(exc).__name__)
    return handled


# ── owner-scoped reads (web requests; database only) ─────────────────────────
_OWNED = "c.id = :id AND c.user_id = :user AND c.status <> 'disconnected'"


async def read_ticker(store: SqlStore, connection_id: int, user_id: str) -> dict:
    """Cached quotes for the connection's watchlist, and a note that someone is looking."""
    ttl, poll = config.market_interest_seconds(), config.market_quote_interval_seconds()
    rows = await store._one(f"""
        WITH c AS (SELECT c.id, c.status FROM mtapi_connections c WHERE {_OWNED}),
        touch AS (
            INSERT INTO mtapi_market_feeds (connection_id, key, interest_until)
            SELECT c.id, k.key, NOW() + make_interval(secs => CAST(:ttl AS double precision)) FROM c, (VALUES ('symbols'), ('quotes')) AS k(key)
            WHERE c.status = 'active'
            ON CONFLICT (connection_id, key) DO UPDATE SET interest_until = EXCLUDED.interest_until
            RETURNING (xmax = 0) AS created)
        SELECT c.status, NOW() AS now, (SELECT bool_or(created) FROM touch) AS created,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = 'quotes') AS feed,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = 'symbols') AS symbols_feed,
               (SELECT jsonb_agg(jsonb_build_object('symbol', q.symbol, 'alias', COALESCE(s.alias, q.alias, q.symbol), 'bid', q.bid::text, 'ask', q.ask::text,
                            'provider_time', q.provider_time, 'fetched_at', q.fetched_at, 'price_precision', s.price_precision, 'session_open', s.session_open)
                        ORDER BY q.symbol)
                FROM mtapi_market_quotes q LEFT JOIN mtapi_market_symbols s ON s.connection_id = q.connection_id AND s.symbol = q.symbol
                WHERE q.connection_id = c.id) AS quotes
        FROM c
    """, {"id": connection_id, "user": user_id, "ttl": ttl})
    if not rows:
        raise NotFound()
    row = rows[0]
    now = row["now"]
    feed = _feed(row["feed"])
    symbols_feed = _feed(row["symbols_feed"])
    quotes = row["quotes"] or []
    watchlist = list((feed or {}).get("watchlist") or [])
    order = {symbol: i for i, symbol in enumerate(watchlist)}
    quotes = sorted((q for q in quotes if not watchlist or q["symbol"] in order), key=lambda q: order.get(q["symbol"], 999))
    # Permission problems can show up on either endpoint; report whichever blocks the ticker.
    blocking = feed if (feed or {}).get("last_error_code") else (symbols_feed if not quotes else feed)
    status, detail = feed_status(blocking, has_data=bool(quotes), now=now, poll_seconds=poll)
    if row["status"] != "active":
        status, detail = ("unavailable", "This connection is not active, so prices are not being refreshed.") if not quotes else ("stale", "This connection is not active; these prices are no longer refreshed.")
    items = []
    for q in quotes:
        item_status = status
        if row["status"] == "active" and status in ("fresh", "stale") and q.get("session_open") is False:
            item_status = "market_closed"
        items.append({"symbol": q["symbol"], "alias": q["alias"], "bid": q["bid"], "ask": q["ask"], "price_precision": q.get("price_precision"),
                      "provider_time": _iso_text(q.get("provider_time")), "fetched_at": _iso_text(q.get("fetched_at")),
                      "session_open": q.get("session_open"), "state": item_status})
    if row["status"] == "active" and items and all(i["state"] == "market_closed" for i in items) and status in ("fresh", "stale"):
        status, detail = "market_closed", "Market closed: these are the last prices the broker published."
    return {"connection_id": connection_id, "price_basis": "bid_ask", "updating": "auto", "poll_interval_seconds": poll,
            "status": status, "detail": detail, "last_success_at": _iso((feed or {}).get("last_success_at")),
            "last_error": (blocking or {}).get("last_error_code"), "items": items, "server_time": _iso(now),
            "_wake_worker": bool(row["created"]) or status == "loading"}


async def _recently_traded_symbols(store: SqlStore, connection_id: int, user_id: str) -> list[str]:
    rows = await store._one(f"""
        SELECT symbol FROM (
            (SELECT p.symbol, 0 AS rank, p.open_time AS t FROM mtapi_positions p JOIN mtapi_connections c ON c.id = p.connection_id
             WHERE {_OWNED} ORDER BY p.open_time DESC LIMIT 1)
            UNION ALL
            (SELECT h.symbol, 1 AS rank, h.close_time AS t FROM mtapi_closed_positions h JOIN mtapi_connections c ON c.id = h.connection_id
             WHERE {_OWNED} ORDER BY h.close_time DESC LIMIT 1)) x ORDER BY rank
    """, {"id": connection_id, "user": user_id})
    return [r["symbol"] for r in rows if r["symbol"]]


async def read_candles(store: SqlStore, connection_id: int, user_id: str, *, symbol: str | None, interval: str | None) -> dict:
    """Cached candles for one instrument and interval. Registers interest so the worker keeps them fresh."""
    interval = interval or DEFAULT_INTERVAL
    if interval not in INTERVAL_SECONDS:
        raise ValueError("unsupported interval")
    ttl, poll = config.market_interest_seconds(), config.market_candle_interval_seconds()
    first = await store._one(f"""
        WITH c AS (SELECT c.id, c.status FROM mtapi_connections c WHERE {_OWNED}),
        touch AS (
            INSERT INTO mtapi_market_feeds (connection_id, key, interest_until)
            SELECT c.id, 'symbols', NOW() + make_interval(secs => CAST(:ttl AS double precision)) FROM c WHERE c.status = 'active'
            ON CONFLICT (connection_id, key) DO UPDATE SET interest_until = EXCLUDED.interest_until RETURNING 1)
        SELECT c.status, NOW() AS now,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = 'symbols') AS symbols_feed,
               (SELECT jsonb_agg(to_jsonb(s) ORDER BY s.symbol) FROM mtapi_market_symbols s WHERE s.connection_id = c.id) AS symbols
        FROM c
    """, {"id": connection_id, "user": user_id, "ttl": ttl})
    if not first:
        raise NotFound()
    head = first[0]
    now, offered = head["now"], head["symbols"] or []
    symbols_feed = _feed(head["symbols_feed"])
    base = {"connection_id": connection_id, "interval": interval, "available_intervals": list(UI_INTERVALS), "updating": "auto",
            "poll_interval_seconds": poll, "price_side": "not_specified_by_provider", "server_time": _iso(now), "candles": [],
            "window": {"count": 0, "limit": config.market_candle_window(), "first_open_time": None, "last_open_time": None, "is_complete_history": False}}
    if not offered:
        status, detail = feed_status(symbols_feed, has_data=False, now=now, poll_seconds=poll)
        if head["status"] != "active":
            status, detail = "unavailable", "This connection is not active, so market data is not being loaded."
        return {**base, "symbol": None, "status": status, "detail": detail, "last_success_at": None,
                "last_error": (symbols_feed or {}).get("last_error_code"), "_wake_worker": status == "loading"}
    if symbol:
        chosen = next((s for s in offered if s["symbol"] == symbol), None)
        if chosen is None:
            raise UnknownSymbol()
    else:
        # No explicit choice: the instrument this account trades (newest open position, else the
        # newest closed trade) if it is offered, otherwise the general default.
        recent = await _recently_traded_symbols(store, connection_id, user_id)
        chosen = next((s for name in recent for s in offered if s["symbol"] == name), None) or choose_chart_symbol(offered)
        if chosen is None:
            return {**base, "symbol": None, "status": "unavailable", "detail": "This account does not offer a currency pair to chart.",
                    "last_success_at": None, "last_error": None, "_wake_worker": False}
    key = f"candles:{chosen['symbol']}:{interval}"
    rows = await store._one(f"""
        WITH c AS (SELECT c.id, c.status FROM mtapi_connections c WHERE {_OWNED}),
        touch AS (
            INSERT INTO mtapi_market_feeds (connection_id, key, symbol, interval, interest_until)
            SELECT c.id, CAST(:key AS text), CAST(:symbol AS text), CAST(:interval AS text), NOW() + make_interval(secs => CAST(:ttl AS double precision))
            FROM c WHERE c.status = 'active'
            ON CONFLICT (connection_id, key) DO UPDATE SET interest_until = EXCLUDED.interest_until
            RETURNING (xmax = 0) AS created),
        trim AS (      -- bounded: only the most recently viewed chart feeds stay alive
            UPDATE mtapi_market_feeds f SET interest_until = NOW()
            WHERE f.connection_id = :id AND f.key LIKE 'candles:%' AND f.key <> CAST(:key AS text) AND f.interest_until > NOW()
              AND f.key NOT IN (SELECT k.key FROM mtapi_market_feeds k WHERE k.connection_id = :id AND k.key LIKE 'candles:%' AND k.key <> CAST(:key AS text)
                                ORDER BY k.interest_until DESC LIMIT :keep)
            RETURNING 1)
        SELECT (SELECT bool_or(created) FROM touch) AS created,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = CAST(:key AS text)) AS feed,
               (SELECT jsonb_agg(jsonb_build_object('t', (EXTRACT(EPOCH FROM k.open_time) * 1000)::bigint, 'o', k.open::text, 'h', k.high::text,
                                                    'l', k.low::text, 'c', k.close::text, 'ticks', k.tick_count) ORDER BY k.open_time)
                FROM (SELECT * FROM mtapi_market_candles m WHERE m.connection_id = c.id AND m.symbol = CAST(:symbol AS text) AND m.interval = CAST(:interval AS text)
                      ORDER BY m.open_time DESC LIMIT :window) k) AS candles
        FROM c
    """, {"id": connection_id, "user": user_id, "key": key, "symbol": chosen["symbol"], "interval": interval, "ttl": ttl,
          "keep": MAX_CHART_FEEDS - 1, "window": config.market_candle_window()})
    if not rows:
        raise NotFound()
    row = rows[0]
    feed = _feed(row["feed"])
    candles = row["candles"] or []
    status, detail = feed_status(feed, has_data=bool(candles), now=now, poll_seconds=poll, session_open=chosen.get("session_open"))
    if head["status"] != "active":
        status, detail = ("unavailable", "This connection is not active, so market data is not being loaded.") if not candles else ("stale", "This connection is not active; this chart is no longer refreshed.")
    return {**base,
            "symbol": {"symbol": chosen["symbol"], "alias": chosen.get("alias") or chosen["symbol"], "description": chosen.get("description"),
                       "type": chosen.get("type"), "price_precision": chosen.get("price_precision"), "session_open": chosen.get("session_open")},
            "candles": candles,
            # The newest ``limit`` candles only. Never presented as the instrument's full history.
            "window": {"count": len(candles), "limit": config.market_candle_window(), "is_complete_history": False,
                       "first_open_time": _ms_iso(candles[0]["t"]) if candles else None, "last_open_time": _ms_iso(candles[-1]["t"]) if candles else None},
            "status": status, "detail": detail, "last_success_at": _iso((feed or {}).get("last_success_at")),
            "last_error": (feed or {}).get("last_error_code"), "_wake_worker": bool(row["created"]) or status == "loading"}


async def read_live(store: SqlStore, connection_id: int, user_id: str, *, instrument: str) -> dict:
    """The latest cached quote for a live instrument (US30), which broker symbol it is, and which
    candle price side has been verified. Registers interest so the worker keeps the quote fresh.
    Database only: however many tabs ask, the worker makes one broker request per interval."""
    if instrument not in LIVE_INSTRUMENTS:
        raise ValueError("unsupported instrument")
    ttl, poll = config.market_interest_seconds(), config.market_live_quote_interval_seconds()
    first = await store._one(f"""
        WITH c AS (SELECT c.id, c.status FROM mtapi_connections c WHERE {_OWNED}),
        touch AS (
            INSERT INTO mtapi_market_feeds (connection_id, key, interest_until)
            SELECT c.id, 'symbols', NOW() + make_interval(secs => CAST(:ttl AS double precision)) FROM c WHERE c.status = 'active'
            ON CONFLICT (connection_id, key) DO UPDATE SET interest_until = EXCLUDED.interest_until RETURNING (xmax = 0) AS created)
        SELECT c.status, NOW() AS now, (SELECT bool_or(created) FROM touch) AS created,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = 'symbols') AS symbols_feed,
               (SELECT jsonb_agg(to_jsonb(s) ORDER BY s.symbol) FROM mtapi_market_symbols s WHERE s.connection_id = c.id) AS symbols
        FROM c
    """, {"id": connection_id, "user": user_id, "ttl": ttl})
    if not first:
        raise NotFound()
    head = first[0]
    now, offered = head["now"], head["symbols"] or []
    base = {"connection_id": connection_id, "instrument": instrument, "mode": "polling", "poll_interval_seconds": poll,
            "server_time": _iso(now), "quote": None, "price_basis": {"side": "unverified", "samples": None},
            "last_success_at": None, "last_error": None}
    if not offered:
        symbols_feed = _feed(head["symbols_feed"])
        status, detail = feed_status(symbols_feed, has_data=False, now=now, poll_seconds=poll)
        if head["status"] != "active":
            status, detail = "unavailable", "This connection is not active, so market data is not being loaded."
        return {**base, "resolution": {"status": "pending", "symbol": None, "candidates": []}, "status": status, "detail": detail,
                "_wake_worker": bool(head["created"]) or status == "loading"}
    resolved = resolve_instrument(offered, instrument)
    chosen = resolved["symbol"]
    resolution = {"status": resolved["status"], "candidates": resolved["candidates"],
                  "symbol": None if chosen is None else {"symbol": chosen["symbol"], "alias": chosen.get("alias") or chosen["symbol"],
                                                        "description": chosen.get("description"), "price_precision": chosen.get("price_precision"),
                                                        "session_open": chosen.get("session_open")}}
    if chosen is None:
        detail = (f"This account offers several instruments that could be {instrument}: "
                  + ", ".join(c["symbol"] for c in resolved["candidates"]) + ". None was picked.") if resolved["status"] == "ambiguous" \
            else f"This account does not offer {instrument} (looked for {', '.join(LIVE_INSTRUMENTS[instrument])})."
        return {**base, "resolution": resolution, "status": "unavailable", "detail": detail, "_wake_worker": False}
    key = f"{LIVE_PREFIX}{instrument}"
    rows = await store._one(f"""
        WITH c AS (SELECT c.id, c.status FROM mtapi_connections c WHERE {_OWNED}),
        touch AS (
            INSERT INTO mtapi_market_feeds (connection_id, key, symbol, interest_until)
            SELECT c.id, CAST(:key AS text), CAST(:symbol AS text), NOW() + make_interval(secs => CAST(:ttl AS double precision))
            FROM c WHERE c.status = 'active'
            ON CONFLICT (connection_id, key) DO UPDATE SET interest_until = EXCLUDED.interest_until, symbol = EXCLUDED.symbol,
                next_due_at = CASE WHEN mtapi_market_feeds.symbol IS DISTINCT FROM EXCLUDED.symbol THEN NOW() ELSE mtapi_market_feeds.next_due_at END
            RETURNING (xmax = 0) AS created)
        SELECT (SELECT bool_or(created) FROM touch) AS created,
               (SELECT to_jsonb(f) FROM mtapi_market_feeds f WHERE f.connection_id = c.id AND f.key = CAST(:key AS text)) AS feed,
               (SELECT jsonb_build_object('bid', q.bid::text, 'ask', q.ask::text, 'provider_time', q.provider_time, 'fetched_at', q.fetched_at)
                FROM mtapi_market_quotes q WHERE q.connection_id = c.id AND q.symbol = CAST(:symbol AS text)) AS quote,
               (SELECT to_jsonb(b) FROM mtapi_market_price_basis b WHERE b.connection_id = c.id AND b.symbol = CAST(:symbol AS text)) AS basis
        FROM c
    """, {"id": connection_id, "user": user_id, "key": key, "symbol": chosen["symbol"], "ttl": ttl})
    if not rows:
        raise NotFound()
    row = rows[0]
    feed, quote, basis = _feed(row["feed"]), row["quote"], row["basis"] or {}
    if feed and feed.get("symbol") != chosen["symbol"]:
        feed = None                     # the broker's symbol changed: nothing fetched for the new one yet
    status, detail = feed_status(feed, has_data=bool(quote), now=now, poll_seconds=poll, session_open=chosen.get("session_open"),
                                 stale_after=max(3 * poll, 20))
    if head["status"] != "active":
        status, detail = ("unavailable", "This connection is not active, so prices are not being refreshed.") if not quote else ("stale", "This connection is not active; this price is no longer refreshed.")
    quote_out = None
    if quote:
        provider = _parse_ts(quote["provider_time"]) if isinstance(quote.get("provider_time"), str) else quote.get("provider_time")
        quote_out = {"bid": quote.get("bid"), "ask": quote.get("ask"), "provider_time": _iso(provider),
                     "provider_time_ms": int(provider.timestamp() * 1000) if provider else None, "fetched_at": _iso_text(quote.get("fetched_at"))}
    samples = {k: int(basis.get(f"{k}_hits") or 0) for k in ("bid", "ask", "mid")}
    samples["miss"] = int(basis.get("misses") or 0)
    return {**base, "resolution": resolution, "quote": quote_out,
            "price_basis": {"side": basis_verdict(basis), "samples": samples, "min_samples": BASIS_MIN_SAMPLES},
            "status": status, "detail": detail, "last_success_at": _iso((feed or {}).get("last_success_at")),
            "last_error": (feed or {}).get("last_error_code"), "_wake_worker": bool(row["created"]) or status == "loading"}


def _parse_ts(value: str) -> datetime:
    """Timestamps that came back inside JSON (to_jsonb): ISO-8601 with 0-6 fractional digits.
    Normalized by hand because datetime.fromisoformat() before Python 3.11 only accepts 3 or 6."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}(?::?\d{2})?)?$", value.strip())
    if not match:
        raise ValueError("unrecognized timestamp")
    base, fraction, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else (zone if ":" in zone else (zone + ":00" if len(zone) == 3 else zone[:3] + ":" + zone[3:]))
    return datetime.fromisoformat(f"{base.replace(' ', 'T')}.{(fraction or '0').ljust(6, '0')}{zone}")


def _feed(value: Any) -> dict | None:
    """A feed row that came back through to_jsonb(): timestamps are ISO strings there."""
    if not value:
        return None
    feed = dict(value)
    for name in ("last_success_at", "last_attempt_at", "interest_until", "next_due_at"):
        if isinstance(feed.get(name), str):
            feed[name] = _parse_ts(feed[name])
    return feed


def _iso_text(value: Any) -> str | None:
    return _iso(_parse_ts(value)) if isinstance(value, str) else _iso(value)


def _ms_iso(value: int) -> str:
    return _iso(datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc))
