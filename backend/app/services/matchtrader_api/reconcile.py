"""Read-only comparison of API-imported closed positions against legacy (extension) trades.

Produces a proposal only. It never merges, deletes or rewrites rows. Matches require the same
symbol, side, close time (to the second) and volume; anything else is reported as unmatched or
ambiguous for human review.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal


def _side(value) -> str:
    return str(value or "").strip().lower()


def _key(symbol, side, when, volume) -> tuple:
    second = when.replace(microsecond=0).isoformat() if when else None
    vol = str(Decimal(str(volume)).normalize()) if volume is not None else None
    return (str(symbol or "").upper(), _side(side), second, vol)


def build_report(api_rows: list[dict], legacy_rows: list[dict]) -> dict:
    legacy_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in legacy_rows:
        legacy_by_key[_key(row["symbol"], row["direction"], row["closed_at"], row["volume"])].append(row)
    matched, ambiguous, api_only = [], [], []
    used_legacy: set = set()
    for row in api_rows:
        candidates = [c for c in legacy_by_key.get(_key(row["symbol"], row["side"], row["close_time"], row["volume"]), []) if c["id"] not in used_legacy]
        if len(candidates) == 1:
            used_legacy.add(candidates[0]["id"])
            api_net = Decimal(str(row["net_profit"])) if row["net_profit"] is not None else None
            legacy_pnl = Decimal(str(candidates[0]["pnl"])) if candidates[0]["pnl"] is not None else None
            matched.append({"uid": row["uid"], "legacy_trade_id": candidates[0]["id"],
                            "pnl_difference": str(api_net - legacy_pnl) if api_net is not None and legacy_pnl is not None else None})
        elif candidates:
            ambiguous.append({"uid": row["uid"], "legacy_trade_ids": [c["id"] for c in candidates]})
        else:
            api_only.append(row["uid"])
    legacy_only = [r["id"] for r in legacy_rows if r["id"] not in used_legacy and not any(r["id"] in a["legacy_trade_ids"] for a in ambiguous)]
    partial_groups = defaultdict(int)
    for row in api_rows:
        partial_groups[row["position_id"]] += 1
    api_net_total = sum((Decimal(str(r["net_profit"])) for r in api_rows if r["net_profit"] is not None), Decimal("0"))
    legacy_total = sum((Decimal(str(r["pnl"])) for r in legacy_rows if r["pnl"] is not None), Decimal("0"))
    return {
        "api_closed_entries": len(api_rows), "legacy_trades": len(legacy_rows),
        "matched": len(matched), "ambiguous": len(ambiguous), "api_only": len(api_only), "legacy_only": len(legacy_only),
        "positions_closed_in_parts": sum(1 for n in partial_groups.values() if n > 1),
        "api_net_profit_total": str(api_net_total), "legacy_pnl_total": str(legacy_total),
        "note": "API total uses netProfit (after commission and swap). Legacy pnl semantics are whatever the extension scraped; a difference is expected until that is confirmed.",
        "matched_detail": matched[:200], "ambiguous_detail": ambiguous[:200], "api_only_uids": api_only[:200], "legacy_only_ids": legacy_only[:200],
        "writes_performed": 0,
    }
