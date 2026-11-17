"""Session-authenticated routes for Match-Trader API connections.

Every route: requires a TaliTrade user session (extension tokens are refused), resolves the
owner from that session only, and answers 404 for a connection the caller does not own.
The whole router answers 404 unless TALITRADE_MT_API_ENABLED is on.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr

from app.core import ingest_auth
from app.core.ingest_auth import Principal
from app.services.matchtrader_api import history_import, market, worker, client as api, config, crypto, engine, ratelimit, read_models, reconcile
from app.services.matchtrader_api.store import IdentityConflict, SqlStore, StaleWriter

logger = logging.getLogger("app.matchtrader_api")


def require_enabled() -> None:
    if not config.api_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/connectors/matchtrader-api", tags=["matchtrader-api"], dependencies=[Depends(require_enabled)])

REQUIRED_CAPABILITIES = ("account", "summary", "positions", "closed_positions")
_CONNECT_ATTEMPTS: dict[str, deque] = defaultdict(deque)
CONNECT_ATTEMPT_LIMIT, CONNECT_ATTEMPT_WINDOW = 5, 600.0


def get_store() -> SqlStore:
    return SqlStore()


def get_read_models() -> read_models.ReadModels:
    return read_models.ReadModels()


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def make_client(token: str, budget: ratelimit.AccountRateBudget | None = None):
    return api.MatchTraderApiClient(token, budget=budget)


class TokenBody(BaseModel):
    # SecretStr keeps the token out of reprs and logs. Length is checked in the handler, not
    # here: a schema constraint would make FastAPI echo the submitted value in its 422 body.
    token: SecretStr


def _submitted_token(body: TokenBody) -> str:
    token = body.token.get_secret_value().strip()
    if not 20 <= len(token) <= 4096 or any(ch.isspace() for ch in token):
        raise HTTPException(status_code=422, detail="That does not look like a Match-Trader API token")
    return token


class BackfillBody(BaseModel):
    """``range``: 30d | 90d | all | custom. ``custom`` (or a bare ``from_ms``) needs ``from_ms``.
    ``all`` uses the account creation time the broker reported; without it a start date is required."""
    from_ms: int | None = Field(default=None, ge=0)
    range: str | None = None


HISTORY_PRESET_DAYS = {"30d": 30, "90d": 90}


def _throttle(user_id: str) -> None:
    now = time.monotonic()
    attempts = _CONNECT_ATTEMPTS[user_id]
    while attempts and now - attempts[0] > CONNECT_ATTEMPT_WINDOW:
        attempts.popleft()
    if len(attempts) >= CONNECT_ATTEMPT_LIMIT:
        raise HTTPException(status_code=429, detail="Too many connection attempts. Try again later.")
    attempts.append(now)


def _platform_host(platform_url: str | None) -> str | None:
    """Identity namespace only. This host is never contacted."""
    try:
        host = (urlparse(str(platform_url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host or None


async def _probe(token: str) -> dict:
    """Validate a token with read-only calls and return the verified identity + capabilities."""
    capabilities: dict[str, bool] = {}
    async with make_client(token) as client:
        try:
            details, account = await asyncio.gather(client.platform_details(), client.account())
        except api.Unauthorized:
            raise HTTPException(status_code=422, detail="Match-Trader rejected this token. Create a new read-only token and try again.") from None
        except api.Forbidden:
            raise HTTPException(status_code=422, detail="This token is not permitted to read the account. API access may not be enabled by your broker.") from None
        except api.RateLimited:
            raise HTTPException(status_code=429, detail="Match-Trader rate limit reached. Try again in a minute.") from None
        except api.MatchTraderApiError:
            raise HTTPException(status_code=502, detail="Could not verify the token with Match-Trader right now.") from None
        capabilities["account"] = True
        now_ms = int(time.time() * 1000)
        probes = {
            "summary": client.account_summary,
            "positions": client.positions,
            "pending_orders": client.pending_orders,
            "closed_positions": lambda: client.closed_positions(now_ms - 60_000, now_ms, limit=1),
            "ledgers": lambda: client.ledgers(now_ms - 60_000, now_ms, limit=1),
        }
        # Side by side: on a slow link the verification time is one round trip, not five.
        answers = await asyncio.gather(*(call() for call in probes.values()), return_exceptions=True)
        for name, answer in zip(probes, answers):
            if not isinstance(answer, BaseException):
                capabilities[name] = True
            elif isinstance(answer, api.Forbidden):
                capabilities[name] = False
            elif isinstance(answer, api.Unauthorized):
                raise HTTPException(status_code=422, detail="Match-Trader rejected this token. Create a new read-only token and try again.") from None
            elif isinstance(answer, api.MatchTraderApiError):
                raise HTTPException(status_code=502, detail="Could not verify the token with Match-Trader right now.") from None
            else:
                raise answer
    return {
        "identity": {
            "platform_host": _platform_host(details.platformUrl) or "unverified",
            "external_login": account.login, "broker_name": details.brokerName, "platform_url": details.platformUrl,
            "account_currency": account.currency, "account_type": account.accountType,
            "account_created_ms": account.createTime,
        },
        "identity_verified": _platform_host(details.platformUrl) is not None,
        "capabilities": capabilities,
    }


def _public(connection: dict, sync_state: dict | None = None, coverage: dict | None = None) -> dict:
    """What a client may see. No ciphertext, generation, lease or token material."""
    def iso(value):
        return value.isoformat() if isinstance(value, datetime) else value

    out = {
        "id": connection["id"], "provider": config.PROVIDER, "status": connection["status"], "status_detail": connection["status_detail"],
        "broker_name": connection["broker_name"], "platform_host": connection["platform_host"], "login": connection["external_login"],
        "currency": connection["account_currency"], "account_type": connection["account_type"],
        "ingestion_mode": connection["ingestion_mode"], "linked_account_id": connection["linked_external_account_id"],
        "capabilities": connection["capabilities"], "token_hint": connection["token_hint"], "created_at": iso(connection["created_at"]),
    }
    if sync_state is not None:
        out["freshness"] = {r: {"last_success_at": iso(s["last_success_at"]), "last_attempt_at": iso(s["last_attempt_at"]),
                                "last_error": s["last_error_code"], "consecutive_failures": s["consecutive_failures"]} for r, s in sync_state.items()}
    if coverage is not None:
        out["history_coverage"] = coverage
    return out


async def _owned(store: SqlStore, connection_id: int, principal: Principal) -> dict:
    connection = await store.get_connection(connection_id)
    if not connection or connection["user_id"] != principal.user_id or connection["status"] == "disconnected":
        raise HTTPException(status_code=404, detail="Connection not found")  # same answer whether it exists or not
    return connection


def _encryptor(token: str, user_id: str, identity: dict):
    return lambda connection_id: crypto.encrypt_token(token, crypto.build_associated_data(
        user_id=user_id, connection_id=connection_id, platform_host=identity["platform_host"], external_login=identity["external_login"]))


@router.post("/connections")
async def connect(body: TokenBody, principal: Principal = Depends(ingest_auth.require_session_principal)):
    token = _submitted_token(body)
    if not crypto.is_configured():
        raise HTTPException(status_code=503, detail="Token storage is not configured on this server")
    if not config.service_status(principal.user_id)["accepting_connections"]:
        # Schema, routes and worker can be live before the form is opened; until then nothing is stored.
        raise HTTPException(status_code=503, detail="New Match-Trader connections are not open on this server yet")
    _throttle(principal.user_id)
    started = time.monotonic()
    probe = await _probe(token)
    verified = time.monotonic()
    identity, capabilities = probe["identity"], probe["capabilities"]
    missing = [name for name in REQUIRED_CAPABILITIES if not capabilities.get(name)]
    status, detail = "active", None
    if not probe["identity_verified"]:
        status, detail = "pending_review", "platform_identity_unverified"  # never synced, never linked
    elif missing:
        status, detail = "forbidden", "missing_capabilities:" + ",".join(missing)
    # Link to an existing TaliTrade account only when this user already owns it. Holding a broker
    # token never transfers ownership of someone else's (or orphaned) TaliTrade records.
    linked = None
    if status == "active" and principal.user_id in await ingest_auth.get_account_owners(identity["external_login"]):
        linked = identity["external_login"]
    store = get_store()
    cutoff = int(time.time() * 1000)
    days = config.initial_backfill_days()
    try:
        # One transaction: the connection, its encrypted credential, the first refresh and the
        # initial history backfill. Nothing else has to be set up by the browser afterwards.
        connection = await store.create_connection(
            user_id=principal.user_id, identity=identity, capabilities=capabilities, status=status, status_detail=detail,
            linked_external_account_id=linked, token_hint=crypto.mask_token(token),
            encrypt=_encryptor(token, principal.user_id, identity), key_version=crypto.key_version_of,
            queue_first_sync=True, backfill_range=(cutoff - days * 86_400_000 - 1, cutoff + 1) if days else None)
    except IdentityConflict:
        logger.warning("matchtrader connect conflict user=%s", principal.user_id)
        raise HTTPException(status_code=409, detail="This trading account is already connected. If it is yours, replace its token instead.") from None
    stored = time.monotonic()
    timings = {"verify_seconds": round(verified - started, 3), "store_seconds": round(stored - verified, 3)}
    logger.info("matchtrader connection created id=%s user=%s status=%s verify_seconds=%s store_seconds=%s",
                connection["id"], principal.user_id, status, timings["verify_seconds"], timings["store_seconds"])
    if status == "active":
        worker.nudge()
    return {"ok": True, "connection": _public(connection), "card": read_models.card_for_new_connection(connection),
            "sync_request": read_models.public_sync_request(connection.get("sync_request")),
            "initial_backfill": {"days": days if connection.get("initial_backfill_resources") else 0,
                                 "resources": connection.get("initial_backfill_resources") or []},
            "timings": timings}


@router.get("/connections")
async def list_connections(response: Response, view: str = "full", principal: Principal = Depends(ingest_auth.require_session_principal)):
    """``view=cards`` is what the dashboard loads on every start: readiness + switcher cards, in
    two queries. The default view adds per-connection sync state and history coverage."""
    _private(response)
    cards = await get_read_models().list_owned(principal.user_id)
    if view == "cards":
        return {"service": config.service_status(principal.user_id), "connections": [{"id": card["id"], "card": card} for card in cards]}
    store = get_store()
    connections = await store.list_connections(principal.user_id)
    cards = {card["id"]: card for card in cards}
    return {"service": config.service_status(principal.user_id),
            "connections": [{**_public(c, await store.get_sync_state(c["id"]), await store.coverage(c["id"])), "card": cards.get(c["id"])} for c in connections]}


@router.get("/connections/{connection_id}/snapshot")
async def get_snapshot(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Everything persisted for one connection. Reads the database only; never contacts the broker."""
    try:
        snapshot = await get_read_models().read_owned_snapshot(connection_id, principal.user_id)
    except read_models.NotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    _private(response)
    return snapshot


@router.get("/connections/{connection_id}/trades")
async def get_trades(connection_id: int, response: Response, limit: int = 50, cursor: str | None = None,
                     principal: Principal = Depends(ingest_auth.require_session_principal)):
    try:
        page = await get_read_models().read_owned_closed_trades(connection_id, principal.user_id, limit=limit, cursor=cursor)
    except read_models.NotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except read_models.BadCursor:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None
    _private(response)
    return page


@router.post("/connections/{connection_id}/rotate")
async def rotate(connection_id: int, body: TokenBody, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    connection = await _owned(store, connection_id, principal)
    token = _submitted_token(body)
    _throttle(principal.user_id)
    probe = await _probe(token)
    identity = probe["identity"]
    if (identity["platform_host"], identity["external_login"]) != (connection["platform_host"], connection["external_login"]):
        raise HTTPException(status_code=409, detail="That token belongs to a different trading account. Nothing was changed.")
    try:
        updated = await store.rotate_credential(connection_id, encrypt=_encryptor(token, principal.user_id, identity),
                                                key_version=crypto.key_version_of, token_hint=crypto.mask_token(token), capabilities=probe["capabilities"],
                                                account_created_ms=identity.get("account_created_ms"))
    except StaleWriter:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    return {"ok": True, "connection": _public(updated)}


@router.post("/connections/{connection_id}/pause")
async def pause(connection_id: int, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    await _owned(store, connection_id, principal)
    if not await store.set_status(connection_id, "paused", "paused_by_user", only_from=("active",)):
        raise HTTPException(status_code=409, detail="Only an active connection can be paused")
    return {"ok": True}


@router.post("/connections/{connection_id}/resume")
async def resume(connection_id: int, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    await _owned(store, connection_id, principal)
    if not await store.set_status(connection_id, "active", None, only_from=("paused",)):
        raise HTTPException(status_code=409, detail="Only a paused connection can be resumed")
    return {"ok": True}


@router.post("/connections/{connection_id}/sync", status_code=202)
async def request_sync(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Queue a refresh. Answers at once with 202; the worker does the broker calls.

    A 202 means "accepted", never "synchronized": poll ``GET .../sync`` (or the snapshot) and read
    ``request.state``: queued -> running -> completed | partial | failed."""
    store = get_store()
    if not config.worker_enabled():
        # Checked first, without touching the database. Says nothing about whether the id exists.
        raise HTTPException(status_code=503, detail="Background sync is not running on this server")
    # One statement: ownership check, "is it active", de-duplication and the insert.
    row = await store.request_sync_owned(connection_id, principal.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")  # same answer whether it exists or not
    if row["connection_status"] != "active":
        raise HTTPException(status_code=409, detail="This connection is not active")
    worker.nudge()
    _private(response)
    return {"accepted": True, "already_pending": not row["created"], "request": read_models.public_sync_request(row)}


@router.get("/connections/{connection_id}/sync")
async def sync_status(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    await _owned(store, connection_id, principal)
    _private(response)
    return {"request": read_models.public_sync_request(await store.get_sync_request(connection_id))}


@router.post("/connections/{connection_id}/backfill")
async def start_backfill(connection_id: int, body: BackfillBody, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Import older closed trades and ledger entries in the background, back to a start point.

    Only the not-yet-requested part is queued; repeating a request queues nothing and never removes
    or duplicates imported rows. Completion is reported "for the requested range": the broker's
    retention depth is not known, so this is never presented as lifetime completeness."""
    store = get_store()
    connection = await _owned(store, connection_id, principal)
    if connection["status"] != "active":
        raise HTTPException(status_code=409, detail="This connection is not active")
    cutoff = int(time.time() * 1000)
    created = connection.get("account_created_at")
    created_ms = int(created.timestamp() * 1000) if created else None
    choice = (body.range or ("custom" if body.from_ms is not None else "")).strip().lower()
    if choice in HISTORY_PRESET_DAYS:
        from_ms, basis = cutoff - HISTORY_PRESET_DAYS[choice] * 86_400_000, choice
    elif choice == "all":
        if created_ms is None:
            raise HTTPException(status_code=409, detail={"reason": "start_date_required",
                                "message": "The broker did not report when this account was created. Choose a start date instead."})
        from_ms, basis = created_ms, "account_created_at"
    elif choice == "custom":
        if body.from_ms is None:
            raise HTTPException(status_code=422, detail="Choose a start date")
        from_ms, basis = body.from_ms, "start_date"
    else:
        raise HTTPException(status_code=422, detail="Choose 30d, 90d, all or a start date")
    if from_ms >= cutoff:
        raise HTTPException(status_code=422, detail="The start date must be in the past")
    if from_ms < 946_684_800_000:
        raise HTTPException(status_code=422, detail="The start date is too far back")
    clamped = False
    if created_ms is not None and from_ms < created_ms:
        from_ms, clamped = created_ms, True          # nothing can have closed before the account existed
    resources = [r for r in ("closed_positions", "ledgers") if (connection["capabilities"] or {}).get(r) is not False]
    planned = await store.request_older_history(connection_id, principal.user_id, from_ms, cutoff, resources)
    if planned is None:
        raise HTTPException(status_code=409, detail="This connection is not active")
    queued = [p["resource"] for p in planned if p["queued"]]
    if queued:
        worker.nudge()
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")   # noqa: E731
    return {"ok": True, "queued": queued, "already_requested": [p["resource"] for p in planned if not p["queued"]],
            "requested_from": iso(from_ms), "requested_to": iso(cutoff), "lower_bound": basis, "clamped_to_account_creation": clamped,
            "account_created_at": iso(created_ms) if created_ms else None, "background_refresh_enabled": config.worker_enabled(),
            "note": "Older history is imported in the background. When it finishes, coverage reads 'complete for the requested range'. "
                    "How far back the broker keeps history is not known; an account creation date is a starting point, not a guarantee."}


class ImportBody(BaseModel):
    retry: bool = False


@router.post("/connections/{connection_id}/history-import")
async def start_history_import(connection_id: int, response: Response, body: ImportBody | None = None,
                               principal: Principal = Depends(ingest_auth.require_session_principal)):
    """One tap: import all closed-trade history the broker returns for this account, back to the
    broker-reported account creation time. Extends any history already queued (the 7-day import
    at connect), so repeating it, or tapping twice, never starts an overlapping job."""
    store = get_store()
    connection = await _owned(store, connection_id, principal)
    if connection["status"] != "active":
        raise HTTPException(status_code=409, detail="This connection is not active")
    if not await history_import.start(store, connection, principal.user_id, retry=bool(body and body.retry)):
        raise HTTPException(status_code=404, detail="Connection not found")
    worker.nudge()
    _private(response)
    return await history_import.status(store, connection_id, principal.user_id)


@router.get("/connections/{connection_id}/history-import")
async def history_import_status(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """What has been imported and which dates have been checked. Counts are committed rows only."""
    out = await history_import.status(get_store(), connection_id, principal.user_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    _private(response)
    return out


@router.get("/connections/{connection_id}/analytics")
async def analytics(connection_id: int, response: Response, from_ms: int | None = None, to_ms: int | None = None, tz: str = "UTC",
                    principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Realized performance over the imported closed entries of one connection (SQL aggregates
    over the whole stored range, not a page)."""
    try:
        result = await get_read_models().read_owned_analytics(connection_id, principal.user_id, from_ms=from_ms, to_ms=to_ms, tz=tz)
    except read_models.NotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except read_models.BadRange as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _private(response)
    return result


@router.get("/connections/{connection_id}/reconciliation")
async def reconciliation(connection_id: int, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    connection = await _owned(store, connection_id, principal)
    linked = connection["linked_external_account_id"]
    if not linked or principal.user_id not in await ingest_auth.get_account_owners(linked):
        return {"linked_account_id": None, "report": None, "note": "No TaliTrade account you own is linked to this connection, so there is nothing to compare."}
    report = reconcile.build_report(await store.closed_positions_for_report(connection_id), await store.legacy_trades_for_report(linked))
    return {"linked_account_id": linked, "report": report}


@router.delete("/connections/{connection_id}")
async def disconnect(connection_id: int, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    await _owned(store, connection_id, principal)
    await store.disconnect(connection_id)
    logger.info("matchtrader connection disconnected id=%s user=%s", connection_id, principal.user_id)
    return {"ok": True, "note": "Syncing stopped and the stored token was deleted. Imported history was kept. "
                                "This does not revoke the token at your broker: revoke it in Match-Trader's API Access screen."}


# ── market data (reads the cache only; the worker does the broker calls) ─────
def require_market() -> None:
    if not config.market_data_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if not config.worker_enabled():
        raise HTTPException(status_code=503, detail="Market data is not being refreshed on this server")


def _market_response(payload: dict, response: Response) -> dict:
    if payload.pop("_wake_worker", False):
        worker.nudge()
    _private(response)
    return payload


@router.get("/connections/{connection_id}/market/ticker", dependencies=[Depends(require_market)])
async def market_ticker(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Bid/ask snapshots for a small watchlist of instruments this account really offers."""
    try:
        return _market_response(await market.read_ticker(get_store(), connection_id, principal.user_id), response)
    except market.NotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None


@router.get("/connections/{connection_id}/market/candles", dependencies=[Depends(require_market)])
async def market_candles(connection_id: int, response: Response, symbol: str | None = None, interval: str | None = None,
                         principal: Principal = Depends(ingest_auth.require_session_principal)):
    """Cached OHLC candles. Without ``symbol`` the server picks EURUSD if the account offers it, else its first FX pair."""
    if interval is not None and interval not in market.UI_INTERVALS:
        raise HTTPException(status_code=422, detail="Unsupported interval")
    if symbol is not None and (len(symbol) > 40 or not symbol.strip()):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    try:
        return _market_response(await market.read_candles(get_store(), connection_id, principal.user_id, symbol=symbol, interval=interval), response)
    except market.NotFound:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except market.UnknownSymbol:
        raise HTTPException(status_code=404, detail="This account does not offer that instrument") from None
