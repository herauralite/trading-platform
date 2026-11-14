"""One synchronization pass for one connection.

Rules this module enforces:
* single writer: work happens only while holding the connection's lease, and every write is
  fenced by generation (see store.StaleWriter);
* an error is never turned into an empty result: the stored position set is replaced only from
  a successful, validated, unfiltered response;
* history sync is independent of position changes, so a trade that opened and closed between
  polls is still imported;
* 401 stops the connection and asks for a new token; 403 disables just that capability;
  429 / exhausted budget pauses that endpoint; other failures are recorded and retried later.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from app.services.matchtrader_api import backfill, client as api, config, crypto, ratelimit
from app.services.matchtrader_api.store import HISTORY_RESOURCES, SqlStore, StaleWriter, now_utc

logger = logging.getLogger("app.matchtrader_api")

ClientFactory = Callable[[str, ratelimit.AccountRateBudget], Any]


def default_client_factory(token: str, budget: ratelimit.AccountRateBudget):
    return api.MatchTraderApiClient(token, budget=budget)


def account_key(connection: dict) -> str:
    return f"{connection['platform_host']}|{connection['external_login']}"


def new_owner() -> str:
    return f"w-{uuid.uuid4().hex[:12]}"


class _Stop(Exception):
    """Abort the rest of this pass (connection no longer usable or writer fenced)."""


def _error_code(exc: Exception) -> str:
    if isinstance(exc, api.Forbidden):
        return "forbidden"
    if isinstance(exc, (api.RateLimited, ratelimit.BudgetExhausted)):
        return "rate_limited"
    if isinstance(exc, api.TransientError):
        return "transient"
    if isinstance(exc, api.ContractError):
        return "contract"
    if isinstance(exc, api.RequestRejected):
        return "rejected"
    return "internal"


async def _run_step(store: SqlStore, connection_id: int, resource: str, outcome: dict, step) -> bool:
    try:
        outcome[resource] = await step() or "ok"
        return True
    except api.Unauthorized:
        await store.set_status(connection_id, "needs_reauth", "unauthorized", only_from=("active",))
        outcome[resource] = "unauthorized"
        raise _Stop() from None
    except StaleWriter:
        outcome[resource] = "fenced"
        raise _Stop() from None
    except (api.MatchTraderApiError, ratelimit.BudgetExhausted) as exc:
        code = _error_code(exc)
        outcome[resource] = code
        await store.record_failure(connection_id, resource, code)
        return False
    except Exception as exc:  # never let one resource break the others; never log payloads
        logger.error("matchtrader sync step failed connection=%s resource=%s error_type=%s", connection_id, resource, type(exc).__name__)
        outcome[resource] = "internal"
        await store.record_failure(connection_id, resource, "internal")
        return False


async def process_interval(store: SqlStore, client, connection_id: int, generation: int, owner: str, interval: dict) -> str:
    """Fetch one queued history range and commit rows + outcome + sub-ranges atomically."""
    low_priority = interval["kind"] != "incremental"
    fetch = client.closed_positions if interval["resource"] == "closed_positions" else client.ledgers
    entries = await fetch(interval["range_from"], interval["range_to"], limit=backfill.HISTORY_LIMIT, low_priority=low_priority)
    verdict = backfill.evaluate(interval["range_from"], interval["range_to"], len(entries))
    children: list[tuple[int, int]] = []
    if verdict == backfill.SPLIT:
        children = backfill.split(interval["range_from"], interval["range_to"])
    elif verdict == backfill.POINTS:
        # Per-millisecond queries are only trustworthy if ``from == to`` is inclusive. Prove it
        # against a row we just received; if the probe comes back empty, record a gap instead.
        stamp = entries[0].closeTime if interval["resource"] == "closed_positions" else entries[0].timestamp
        probe = await fetch(stamp, stamp, limit=1, low_priority=low_priority)
        if probe:
            children, verdict = backfill.points(interval["range_from"], interval["range_to"]), backfill.SPLIT
        else:
            verdict = backfill.GAP
    await store.complete_interval(connection_id, generation, owner, interval, entries, verdict, children)
    return verdict


async def sync_connection(store: SqlStore, connection_id: int, *, owner: str | None = None, client_factory: ClientFactory = default_client_factory,
                          live: bool = True, history: bool = True, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> dict:
    owner = owner or new_owner()
    connection = await store.get_connection(connection_id)
    if not connection or connection["status"] != "active":
        return {"skipped": connection["status"] if connection else "missing"}
    generation = await store.acquire_lease(connection_id, owner, config.LEASE_SECONDS)
    if generation is None:
        return {"skipped": "busy"}
    outcome: dict[str, Any] = {}
    try:
        credential = await store.get_credential(connection_id)
        if not credential or credential["generation"] != generation:
            return {"skipped": "credential_changed"}
        try:
            token = crypto.decrypt_token(credential["ciphertext"], crypto.build_associated_data(
                user_id=connection["user_id"], connection_id=connection_id,
                platform_host=connection["platform_host"], external_login=connection["external_login"]))
        except crypto.TokenCryptoError:
            await store.set_status(connection_id, "error", "credential_unreadable", only_from=("active",))
            return {"skipped": "credential_unreadable"}
        capabilities = connection.get("capabilities") or {}
        budget = ratelimit.budget_for(account_key(connection))
        async with client_factory(token, budget) as client:
            del token
            try:
                if live:
                    async def summary_step():
                        summary = await client.account_summary()
                        await store.save_summary(connection_id, generation, owner, summary, now_utc())

                    async def positions_step():
                        fetched_at = now_utc()
                        positions = await client.positions()  # raises on any failure: nothing is cleared
                        await store.replace_positions(connection_id, generation, owner, positions, fetched_at)

                    async def pending_step():
                        fetched_at = now_utc()
                        orders = await client.pending_orders()
                        await store.replace_pending_orders(connection_id, generation, owner, orders, fetched_at)

                    await _run_step(store, connection_id, "summary", outcome, summary_step)
                    await _run_step(store, connection_id, "positions", outcome, positions_step)
                    if capabilities.get("pending_orders", True):
                        await _run_step(store, connection_id, "pending_orders", outcome, pending_step)
                    await store.acquire_lease(connection_id, owner, config.LEASE_SECONDS)  # renew

                if history:
                    state = await store.get_sync_state(connection_id)
                    cutoff = now_ms()
                    for resource in HISTORY_RESOURCES:
                        if capabilities.get(resource) is False:
                            outcome[resource] = "not_permitted"
                            continue

                        async def history_step(resource=resource):
                            cursor = (state.get(resource) or {}).get("cursor_ms")
                            start = (cursor - config.history_overlap_seconds() * 1000) if cursor else cutoff - 24 * 3600 * 1000
                            # widened by 1 ms each side: range boundary inclusivity is undocumented
                            await store.enqueue_incremental(connection_id, generation, owner, resource, start - 1, cutoff + 1)
                            if config.reconcile_window_seconds():
                                await store.enqueue_reconcile_if_due(connection_id, resource, cutoff - config.reconcile_window_seconds() * 1000 - 1,
                                                                     cutoff + 1, config.reconcile_every_seconds())
                            slots = 1 + config.backfill_intervals_per_cycle()
                            verdicts = []
                            for interval in await store.next_intervals(connection_id, resource, slots):
                                verdicts.append(await process_interval(store, client, connection_id, generation, owner, interval))
                                await store.acquire_lease(connection_id, owner, config.LEASE_SECONDS)  # renew during long backfills
                            return "gap" if backfill.GAP in verdicts else "ok"

                        await _run_step(store, connection_id, resource, outcome, history_step)
            except _Stop:
                pass
    finally:
        await store.release_lease(connection_id, owner)
    return outcome
