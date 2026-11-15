"""One synchronization pass for one connection.

Rules this module enforces:
* single writer: work happens only while holding the connection's lease, and every write is
  fenced by generation (see store.StaleWriter);
* an error is never turned into an empty result: the stored position set is replaced only from
  a successful, validated, unfiltered response;
* history sync is independent of position changes, so a trade that opened and closed between
  polls is still imported;
* 401 stops the connection and asks for a new token; 403 disables just that capability;
  429 / exhausted budget pauses that endpoint; other failures are recorded and retried later;
* live data outranks history: the live resources are refreshed whenever they fall due, even
  while a history request to the broker is still outstanding, and backfill that runs out of
  rate budget simply waits for the next pass;
* history is fair: recent windows, backfill and reconcile ranges of both resources take turns
  (see HISTORY_TURNS), so slow passes cannot starve older work behind newly arriving windows;
* the history time budget covers planning, database work and nested live refreshes. It is a
  soft budget: no new unit starts after it, but a unit that has started (one broker request
  plus one database statement) finishes. One unit always runs so every pass makes progress;
* a pass uses one database connection and a handful of single-statement operations (see
  store.FastOps): the number of database round trips is what makes a pass slow on a slow link;
* a queued "refresh now" request is claimed, run and reported by the lease holder.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable

from app.services.matchtrader_api import backfill, client as api, config, crypto, ratelimit, read_models
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


_REQUEST_STATES = {"ok": "completed", "partial": "partial"}


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


# Order in which queued history work takes turns. Recent windows get every other turn so new
# trades still show up promptly; backfill and reconcile ranges get the turns in between. A class
# with nothing queued does not use up a turn.
HISTORY_TURNS = (
    ("closed_positions", "incremental"), ("closed_positions", "backfill"),
    ("ledgers", "incremental"), ("ledgers", "backfill"),
    ("closed_positions", "incremental"), ("closed_positions", "reconcile"),
    ("ledgers", "incremental"), ("ledgers", "reconcile"),
)
_turn: dict[int, int] = {}   # connection id -> next position in HISTORY_TURNS (in-process; losing it only resets the rotation)


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, api.Unauthorized):
        return "unauthorized"
    if isinstance(exc, (api.MatchTraderApiError, ratelimit.BudgetExhausted)):
        return _error_code(exc)
    return "internal"


async def process_interval(store: SqlStore, client, connection_id: int, generation: int, owner: str, interval: dict, *, while_waiting=None) -> str:
    """Fetch one queued history range and commit rows + outcome + sub-ranges atomically.

    ``while_waiting`` (optional) is awaited in a loop while the broker request is outstanding;
    it is how live data keeps refreshing behind a slow history response."""
    low_priority = interval["kind"] != "incremental"
    fetch = client.closed_positions if interval["resource"] == "closed_positions" else client.ledgers

    async def call(range_from, range_to, limit):
        request = asyncio.ensure_future(fetch(range_from, range_to, limit=limit, low_priority=low_priority))
        try:
            while while_waiting is not None and not request.done():
                await while_waiting(request)
            return await request
        finally:
            if not request.done():
                request.cancel()

    entries = await call(interval["range_from"], interval["range_to"], backfill.HISTORY_LIMIT)
    verdict = backfill.evaluate(interval["range_from"], interval["range_to"], len(entries))
    children: list[tuple[int, int]] = []
    if verdict == backfill.SPLIT:
        children = backfill.split(interval["range_from"], interval["range_to"])
    elif verdict == backfill.POINTS:
        # Per-millisecond queries are only trustworthy if ``from == to`` is inclusive. Prove it
        # against a row we just received; if the probe comes back empty, record a gap instead.
        stamp = entries[0].closeTime if interval["resource"] == "closed_positions" else entries[0].timestamp
        probe = await call(stamp, stamp, 1)
        if probe:
            children, verdict = backfill.points(interval["range_from"], interval["range_to"]), backfill.SPLIT
        else:
            verdict = backfill.GAP
    await store.complete_interval(connection_id, generation, owner, interval, entries, verdict, children, ttl_seconds=config.LEASE_SECONDS)
    return verdict


def _request_verdict(outcome: dict) -> dict:
    """The stored result for a "refresh now" request: completed | partial | failed (+ reason)."""
    live_only = {name: value for name, value in outcome.items() if name in ("skipped", "summary", "positions", "pending_orders")}
    verdict = read_models.interpret_sync_outcome(live_only or {"skipped": "not_run"})
    state = _REQUEST_STATES.get(verdict["state"], "failed")
    return {"state": state, "detail": None if state != "failed" else (verdict["detail"] or verdict["state"]), "resources": verdict["resources"]}


async def sync_connection(store: SqlStore, connection_id: int, *, owner: str | None = None, client_factory: ClientFactory = default_client_factory,
                          live: bool = True, history: bool = True, now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                          metrics: dict | None = None, schedule: bool = False) -> dict:
    """One pass. ``metrics`` (optional) receives timings and whether history work is still queued.

    With ``schedule`` (the worker sets it) the pass also sets the connection's next due times in
    its final statement: live data is due one live interval after this pass STARTED fetching it,
    not one interval after a slow pass finished."""
    owner = owner or new_owner()
    metrics = metrics if metrics is not None else {}
    started = time.monotonic()
    async with store.session() as session:
        try:
            return await _run_pass(store, session, connection_id, owner, client_factory, live, history, now_ms, metrics, schedule)
        finally:
            metrics["pass_seconds"] = round(time.monotonic() - started, 3)
            metrics["db_calls"], metrics["db_seconds"] = session.db_calls, round(session.db_seconds, 3)
            # Timings only (no payloads, no identifiers beyond the connection id).
            logger.info("matchtrader pass connection=%s seconds=%s live_seconds=%s history_seconds=%s intervals=%s live_refreshes_during_history=%s "
                        "backlog=%s db_calls=%s db_seconds=%s broker_calls=%s broker_seconds=%s broker_max_seconds=%s",
                        connection_id, metrics["pass_seconds"], metrics.get("live_seconds"), metrics.get("history_seconds"),
                        metrics.get("history_intervals", 0), metrics.get("live_refreshes_during_history", 0), metrics.get("history_backlog", False),
                        metrics["db_calls"], metrics["db_seconds"], metrics.get("broker_calls", 0), metrics.get("broker_seconds", 0), metrics.get("broker_max_seconds", 0))


async def _run_pass(store, session, connection_id, owner, client_factory, live, history, now_ms, metrics, schedule) -> dict:
    opened = await store.open_pass(connection_id, owner, config.LEASE_SECONDS, claim_request=live)
    if opened is None:
        connection = await store.get_connection(connection_id)
        if not connection or connection["status"] != "active":
            return {"skipped": connection["status"] if connection else "missing"}
        return {"skipped": "busy"}
    generation = int(opened["generation"])
    request = {"request_id": opened["request_id"]} if opened.get("request_id") is not None else None
    outcome: dict[str, Any] = {}
    released = False            # set once a statement has released the lease (and scheduled the next run)
    live_fetch_started: float | None = None
    # Spreads accounts apart a little (at most 5% of the interval, capped at 1.5 s) so the cadence stays close to the interval.
    live_jitter = (time.monotonic() % 1.0) * min(1.5, config.live_interval_seconds() * 0.05) if schedule else 0.0

    def next_live_in() -> float | None:
        """Seconds from now until live data is due again: one interval after the last fetch began."""
        if not schedule or live_fetch_started is None:
            return None
        return max(0.0, config.live_interval_seconds() + live_jitter - (time.monotonic() - live_fetch_started))

    async def finish_open_request(state: str, detail: str | None, resources: dict | None = None) -> None:
        """Report the request right now, without needing the lease (used when the lease is gone:
        the connection just stopped being active)."""
        nonlocal request
        if request:
            await store.finish_sync_request(connection_id, request["request_id"], state, detail, resources or {})
            request = None

    try:
        if request and (opened.get("request_attempts") or 0) > config.SYNC_REQUEST_MAX_ATTEMPTS:
            # A pass kept dying with this request open. Stop retrying it; routine polling continues.
            await finish_open_request("failed", "internal")
        if not opened.get("ciphertext") or opened.get("credential_generation") != generation:
            outcome = {"skipped": "credential_changed"}
            return outcome
        try:
            token = crypto.decrypt_token(opened["ciphertext"], crypto.build_associated_data(
                user_id=opened["user_id"], connection_id=connection_id,
                platform_host=opened["platform_host"], external_login=opened["external_login"]))
        except crypto.TokenCryptoError:
            await store.set_status(connection_id, "error", "credential_unreadable", only_from=("active",), cancel_requests=False)
            outcome = {"skipped": "credential_unreadable"}
            await finish_open_request("failed", "credential_unreadable")
            return outcome
        capabilities = opened.get("capabilities") or {}
        budget = ratelimit.budget_for(account_key(opened))
        async with client_factory(token, budget) as client:
            del token

            async def live_pass(into: dict, *, final: bool) -> None:
                """Three broker reads side by side, then ONE statement that stores whatever succeeded."""
                nonlocal request, released, live_fetch_started
                live_fetch_started = time.monotonic()
                wanted = [("summary", client.account_summary), ("positions", client.positions)]
                if capabilities.get("pending_orders", True):
                    wanted.append(("pending_orders", client.pending_orders))
                fetched_at = {name: now_utc() for name, _ in wanted}
                answers = await asyncio.gather(*(call() for _, call in wanted), return_exceptions=True)
                values: dict[str, Any] = {}
                for (name, _), answer in zip(wanted, answers):
                    if isinstance(answer, BaseException):
                        if not isinstance(answer, Exception):
                            raise answer
                        into[name] = _failure_code(answer)
                        if into[name] == "internal":
                            logger.error("matchtrader sync step failed connection=%s resource=%s error_type=%s", connection_id, name, type(answer).__name__)
                    else:
                        into[name], values[name] = "ok", answer
                if "unauthorized" in into.values():
                    await store.set_status(connection_id, "needs_reauth", "unauthorized", only_from=("active",), cancel_requests=False)
                    verdict = _request_verdict(into)
                    await finish_open_request(verdict["state"], verdict["detail"], verdict["resources"])
                    raise _Stop()
                finishing = dict(request, **_request_verdict(into)) if request else None
                try:
                    await store.save_live(connection_id, generation, owner, config.LEASE_SECONDS,
                                          summary=values.get("summary"), positions=values.get("positions"), pending=values.get("pending_orders"),
                                          results={name: into[name] for name, _ in wanted}, fetched_at=fetched_at, finish_request=finishing,
                                          release=final, live_in=next_live_in())
                except StaleWriter:
                    for name, _ in wanted:
                        into[name] = "fenced"
                    raise _Stop() from None
                request = None            # reported together with the data it describes
                released = released or final

            try:
                if live:
                    t = time.monotonic()
                    await live_pass(outcome, final=not history)
                    metrics["live_seconds"] = round(time.monotonic() - t, 3)
                if history:
                    await _history_pass(store, client, opened, generation, owner, capabilities, outcome, metrics, now_ms,
                                        live_pass, live_ran=live)
            except _Stop:
                pass
            finally:
                timings = list(getattr(client, "timings", ()))
                metrics["broker_calls"] = len(timings)
                metrics["broker_seconds"] = round(sum(seconds for _, seconds in timings), 3)
                metrics["broker_max_seconds"] = round(max((seconds for _, seconds in timings), default=0.0), 3)
    finally:
        if not released:
            history_in = None
            if schedule and history:
                wait = config.history_backlog_interval_seconds() if metrics.get("history_backlog") else config.history_interval_seconds()
                history_in = wait + live_jitter
            try:
                await store.close_pass(connection_id, owner, live_in=next_live_in(), history_in=history_in,
                                       finish_request=dict(request, **_request_verdict(outcome)) if request else None)
                metrics["scheduled"] = schedule and (history_in is not None or next_live_in() is not None)
            except Exception as exc:  # the lease simply expires; an open request is retried by the next lease holder
                logger.error("matchtrader pass not closed connection=%s error_type=%s", connection_id, type(exc).__name__)
        else:
            metrics["scheduled"] = schedule
    return outcome


async def _history_pass(store, client, opened, generation, owner, capabilities, outcome, metrics, now_ms, live_pass, *, live_ran: bool) -> None:
    connection_id = opened["id"]
    t0 = time.monotonic()
    deadline = t0 + config.history_pass_seconds()            # covers planning, database work and nested live refreshes
    if live_ran:
        live_due_at = t0 + config.live_interval_seconds()
    else:
        live_due_at = t0 + max(0.0, float(opened.get("live_due_in") or 0.0))

    async def refresh_live_if_due() -> None:
        nonlocal live_due_at
        if live_pass is None or time.monotonic() < live_due_at:
            return
        refreshed: dict[str, Any] = {}
        live_due_at = time.monotonic() + config.live_interval_seconds()
        await live_pass(refreshed, final=False)
        outcome.update({name: value for name, value in refreshed.items() if value != "ok" or name not in outcome})
        metrics["live_refreshes_during_history"] = metrics.get("live_refreshes_during_history", 0) + 1

    async def while_waiting(request: asyncio.Future) -> None:
        """Called while a history request is outstanding: wait for it, but not past the moment
        live data falls due. Then refresh live data and come back to waiting."""
        if live_pass is None:
            await asyncio.wait({request})
            return
        await asyncio.wait({request}, timeout=max(0.0, live_due_at - time.monotonic()))
        if not request.done():
            await refresh_live_if_due()

    permitted = [r for r in HISTORY_RESOURCES if capabilities.get(r) is not False]
    for resource in HISTORY_RESOURCES:
        if resource not in permitted:
            outcome[resource] = "not_permitted"
    if not permitted:
        return
    cutoff = now_ms()
    reconcile = config.reconcile_window_seconds()
    try:
        await store.plan_history(connection_id, generation, owner, [{
            "resource": resource, "first_start": cutoff - 24 * 3600 * 1000, "cutoff": cutoff, "overlap": config.history_overlap_seconds() * 1000,
            # windows are widened by 1 ms each side in SQL: range boundary inclusivity is undocumented
            "reconcile_from": (cutoff - reconcile * 1000 - 1) if reconcile else None, "reconcile_every": config.reconcile_every_seconds(),
        } for resource in permitted])
    except StaleWriter:
        for resource in permitted:
            outcome[resource] = "fenced"
        raise _Stop() from None
    per_class = 1 + config.backfill_intervals_per_cycle()
    queued: dict[tuple[str, str], deque] = {}
    for row in await store.history_candidates(connection_id, per_class):
        queued.setdefault((row["resource"], row["kind"]), deque()).append(row)
    status = {resource: "ok" for resource in permitted}
    blocked: set[str] = set()                # resources that failed in this pass: no more units for them
    low_priority_paused = False              # backfill/reconcile ran out of rate budget
    max_units = len(permitted) * per_class
    done = 0
    turn = _turn.get(connection_id, 0)
    idle_turns = 0
    try:
        while done < max_units and idle_turns < len(HISTORY_TURNS):
            resource, kind = HISTORY_TURNS[turn % len(HISTORY_TURNS)]
            turn += 1
            pending = queued.get((resource, kind))
            if not pending or resource in blocked or (low_priority_paused and kind != "incremental"):
                idle_turns += 1
                continue
            if done and time.monotonic() >= deadline:
                turn -= 1                    # out of budget: this class keeps its turn for the next pass
                break
            idle_turns = 0
            interval = pending.popleft()
            await refresh_live_if_due()
            try:
                verdict = await process_interval(store, client, connection_id, generation, owner, interval, while_waiting=while_waiting)
            except _Stop:
                raise
            except api.Unauthorized:
                await store.set_status(connection_id, "needs_reauth", "unauthorized", only_from=("active",), cancel_requests=False)
                status[resource] = "unauthorized"
                raise _Stop() from None
            except StaleWriter:
                status[resource] = "fenced"
                raise _Stop() from None
            except ratelimit.BudgetExhausted as exc:
                if kind != "incremental":
                    low_priority_paused = True    # backfill yields to live traffic; not an error
                    continue
                status[resource] = _error_code(exc)
                blocked.add(resource)
                await store.record_failure(connection_id, resource, status[resource])
                continue
            except api.MatchTraderApiError as exc:
                status[resource] = _error_code(exc)
                blocked.add(resource)
                await store.record_failure(connection_id, resource, status[resource])
                continue
            except Exception as exc:  # never let one resource break the other; never log payloads
                logger.error("matchtrader sync step failed connection=%s resource=%s error_type=%s", connection_id, resource, type(exc).__name__)
                status[resource] = "internal"
                blocked.add(resource)
                await store.record_failure(connection_id, resource, "internal")
                continue
            done += 1
            if verdict == backfill.GAP:
                status[resource] = "gap"
    finally:
        _turn[connection_id] = turn % len(HISTORY_TURNS)
        outcome.update(status)
        metrics["history_seconds"] = round(time.monotonic() - t0, 3)
        metrics["history_intervals"] = done
    # More to do if anything fetched for this pass was left, or a unit may have queued sub-ranges.
    metrics["history_backlog"] = any(queued.values()) or await store.has_queued_intervals(connection_id)
