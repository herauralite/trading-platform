"""Read-only smoke test against the real Match-Trader Trader API.

Usage (from backend/):
    python -m scripts.matchtrader_api_smoke            # prompts for the token, hidden
    MT_API_SMOKE_TOKEN=... python -m scripts.matchtrader_api_smoke

Use a DEMO account and a read-only token you created yourself. The token is read from a hidden
prompt or the environment, never from the command line, and is never printed or stored. Only
GET requests on the allowlisted read paths are sent. Output shows field NAMES, counts and
status codes - not balances, logins or trade details - so it is safe to paste into a report.
"""
import asyncio
import getpass
import os
import time

from app.services.matchtrader_api import client as api


def shape(model) -> str:
    return ", ".join(sorted(model.raw)) if model is not None else "-"


async def main() -> int:
    token = os.getenv("MT_API_SMOKE_TOKEN") or getpass.getpass("Match-Trader read-only API token (input hidden): ")
    token = token.strip()
    if not token:
        print("No token supplied.")
        return 2
    now = int(time.time() * 1000)
    week = 7 * 86_400_000
    async with api.MatchTraderApiClient(token) as client:
        del token
        checks = [
            ("platform-details", client.platform_details, lambda r: f"fields: {shape(r)}; platformUrl present: {bool(r.platformUrl)}"),
            ("account", client.account, lambda r: f"fields: {shape(r)}; accessRight={r.accessRight}; accountType={r.accountType}"),
            ("account-summary", client.account_summary, lambda r: f"fields: {shape(r)}; margin==0: {r.margin == 0}; freeFunds present: {r.freeFunds is not None}"),
            ("positions", client.positions, lambda r: f"{len(r)} open; fields: {shape(r[0]) if r else '-'}"),
            ("pending-orders", client.pending_orders, lambda r: f"{len(r)} pending; fields: {shape(r[0]) if r else '-'}"),
            ("closed-positions (7d)", lambda: client.closed_positions(now - week, now, limit=500),
             lambda r: f"{len(r)} entries; distinct positions: {len({e.id for e in r})}; saturated: {len(r) >= 500}; newest-first: {all(a.closeTime >= b.closeTime for a, b in zip(r, r[1:]))}; fields: {shape(r[0]) if r else '-'}"),
            ("ledgers (7d)", lambda: client.ledgers(now - week, now, limit=500),
             lambda r: f"{len(r)} entries; types: {sorted({str(e.type) for e in r})}; fields: {shape(r[0]) if r else '-'}"),
        ]
        failures = 0
        for name, call, describe in checks:
            try:
                print(f"OK    {name:24} {describe(await call())}")
            except api.MatchTraderApiError as exc:
                failures += 1
                print(f"FAIL  {name:24} {type(exc).__name__} status={exc.status} type={exc.problem_type}")
    print("\nAll requests were GET. Nothing was written anywhere." + ("" if not failures else f"  {failures} check(s) failed."))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
