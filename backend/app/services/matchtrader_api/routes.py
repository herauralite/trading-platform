"""Session-authenticated routes for Match-Trader API connections.

Every route: requires a TaliTrade user session (extension tokens are refused), resolves the
owner from that session only, and answers 404 for a connection the caller does not own.
The whole router answers 404 unless TALITRADE_MT_API_ENABLED is on.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr

from app.core import ingest_auth
from app.core.ingest_auth import Principal
from app.services.matchtrader_api import worker, client as api, config, crypto, engine, ratelimit, read_models, reconcile
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
    from_ms: int = Field(ge=0)


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
            details = await client.platform_details()
            account = await client.account()
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
        for name, call in probes.items():
            try:
                await call()
                capabilities[name] = True
            except api.Forbidden:
                capabilities[name] = False
            except api.Unauthorized:
                raise HTTPException(status_code=422, detail="Match-Trader rejected this token. Create a new read-only token and try again.") from None
            except api.MatchTraderApiError:
                raise HTTPException(status_code=502, detail="Could not verify the token with Match-Trader right now.") from None
    return {
        "identity": {
            "platform_host": _platform_host(details.platformUrl) or "unverified",
            "external_login": account.login, "broker_name": details.brokerName, "platform_url": details.platformUrl,
            "account_currency": account.currency, "account_type": account.accountType,
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
    if not config.service_status()["accepting_connections"]:
        # Schema, routes and worker can be live before the form is opened; until then nothing is stored.
        raise HTTPException(status_code=503, detail="New Match-Trader connections are not open on this server yet")
    _throttle(principal.user_id)
    probe = await _probe(token)
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
    try:
        connection = await store.create_connection(
            user_id=principal.user_id, identity=identity, capabilities=capabilities, status=status, status_detail=detail,
            linked_external_account_id=linked, token_hint=crypto.mask_token(token),
            encrypt=_encryptor(token, principal.user_id, identity), key_version=crypto.key_version_of)
    except IdentityConflict:
        logger.warning("matchtrader connect conflict user=%s", principal.user_id)
        raise HTTPException(status_code=409, detail="This trading account is already connected. If it is yours, replace its token instead.") from None
    logger.info("matchtrader connection created id=%s user=%s status=%s", connection["id"], principal.user_id, status)
    request = None
    if status == "active":   # the first refresh is queued for the worker; the browser never waits on the broker
        request, _ = await store.request_sync(connection["id"])
        worker.nudge()
    return {"ok": True, "connection": _public(connection), "sync_request": read_models.public_sync_request(request)}


@router.get("/connections")
async def list_connections(response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    connections = await store.list_connections(principal.user_id)
    cards = {card["id"]: card for card in await get_read_models().list_owned(principal.user_id)}
    _private(response)
    return {"service": config.service_status(),
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
                                                key_version=crypto.key_version_of, token_hint=crypto.mask_token(token), capabilities=probe["capabilities"])
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
    connection = await _owned(store, connection_id, principal)
    if connection["status"] != "active":
        raise HTTPException(status_code=409, detail="This connection is not active")
    if not config.worker_enabled():
        raise HTTPException(status_code=503, detail="Background sync is not running on this server")
    request, created = await store.request_sync(connection_id)
    if request is None:
        raise HTTPException(status_code=409, detail="This connection is not active")
    worker.nudge()
    _private(response)
    return {"accepted": True, "already_pending": not created, "request": read_models.public_sync_request(request)}


@router.get("/connections/{connection_id}/sync")
async def sync_status(connection_id: int, response: Response, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    await _owned(store, connection_id, principal)
    _private(response)
    return {"request": read_models.public_sync_request(await store.get_sync_request(connection_id))}


@router.post("/connections/{connection_id}/backfill")
async def start_backfill(connection_id: int, body: BackfillBody, principal: Principal = Depends(ingest_auth.require_session_principal)):
    store = get_store()
    connection = await _owned(store, connection_id, principal)
    if connection["status"] != "active":
        raise HTTPException(status_code=409, detail="This connection is not active")
    cutoff = int(time.time() * 1000)
    if body.from_ms >= cutoff:
        raise HTTPException(status_code=422, detail="from_ms must be in the past")
    queued = []
    for resource in ("closed_positions", "ledgers"):
        if (connection["capabilities"] or {}).get(resource) is False:
            continue
        if await store.enqueue_backfill(connection_id, resource, body.from_ms - 1, cutoff + 1):
            queued.append(resource)
    if queued:
        worker.nudge()
    return {"ok": True, "queued": queued, "requested_from": datetime.fromtimestamp(body.from_ms / 1000, tz=timezone.utc).isoformat(),
            "requested_to": datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc).isoformat(), "background_refresh_enabled": config.worker_enabled(),
            "note": "History is imported in the background. Coverage is reported per resource; it is only 'complete' for the range you requested."}


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
