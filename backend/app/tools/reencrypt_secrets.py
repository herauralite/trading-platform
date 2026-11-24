"""Re-encrypt every stored credential from SECRET_KEY_PREVIOUS to SECRET_KEY (part of rotating SECRET_KEY).

    SECRET_KEY=<new> SECRET_KEY_PREVIOUS=<old> python -m app.tools.reencrypt_secrets           # dry run
    SECRET_KEY=<new> SECRET_KEY_PREVIOUS=<old> python -m app.tools.reencrypt_secrets --apply   # commit

Finds Fernet tokens (they start with "gAAAAA") in every text/varchar/jsonb column of the public schema,
including tokens nested inside JSON. A token is rewritten only if it does NOT decrypt under SECRET_KEY and DOES
decrypt under SECRET_KEY_PREVIOUS. Everything happens in one transaction; a dry run rolls back. Output is counts
per table.column only: no values, keys or plaintext are ever printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.services.secret_crypto import PREVIOUS_ENV, fernet_for

PREFIX = "gAAAAA"


class Rotator:
    def __init__(self, current: Fernet, previous: Fernet):
        self.current, self.previous = current, previous
        self.counts: Counter[tuple[str, str]] = Counter()

    def value(self, where: str, token: str) -> str:
        try:
            self.current.decrypt(token.encode())
            self.counts[(where, "already_current")] += 1
            return token
        except InvalidToken:
            pass
        try:
            plain = self.previous.decrypt(token.encode())
        except InvalidToken:
            self.counts[(where, "undecryptable")] += 1
            return token
        self.counts[(where, "rotated")] += 1
        return self.current.encrypt(plain).decode()

    def walk(self, where: str, node: Any) -> Any:
        if isinstance(node, str):
            return self.value(where, node) if node.startswith(PREFIX) else node
        if isinstance(node, list):
            return [self.walk(where, v) for v in node]
        if isinstance(node, dict):
            return {k: self.walk(where, v) for k, v in node.items()}
        return node


async def _candidate_columns(conn) -> dict[str, list[tuple[str, str]]]:
    rows = await conn.execute(text("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type IN ('text', 'character varying', 'jsonb', 'json')
        ORDER BY table_name, ordinal_position"""))
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append((column, dtype))
    return tables


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def rotate(engine, current: Fernet, previous: Fernet, *, apply: bool) -> Rotator:
    rot = Rotator(current, previous)
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            for table, columns in (await _candidate_columns(conn)).items():
                selects = ", ".join(f"{_q(c)}::text" for c, _ in columns)
                where = " OR ".join(f"{_q(c)}::text LIKE '%{PREFIX}%'" for c, _ in columns)
                result = await conn.execute(text(f"SELECT ctid::text, {selects} FROM {_q(table)} WHERE {where} FOR UPDATE"))
                for row in result.fetchall():
                    changes: dict[str, tuple[str, str]] = {}
                    for (column, dtype), raw in zip(columns, row[1:]):
                        if raw is None or PREFIX not in raw:
                            continue
                        label = f"{table}.{column}"
                        if dtype in ("jsonb", "json"):
                            new = json.dumps(rot.walk(label, json.loads(raw)))
                            if json.loads(new) != json.loads(raw):
                                changes[column] = (new, dtype)
                        else:
                            new = rot.value(label, raw) if raw.startswith(PREFIX) else raw
                            if new != raw:
                                changes[column] = (new, dtype)
                    if changes:
                        sets = ", ".join(
                            f"{_q(c)} = CAST(:v{i} AS {d})" if d in ("jsonb", "json") else f"{_q(c)} = :v{i}"
                            for i, (c, (_, d)) in enumerate(changes.items()))
                        params = {f"v{i}": v for i, (_, (v, _)) in enumerate(changes.items())}
                        params["ctid"] = row[0]
                        await conn.execute(text(f"UPDATE {_q(table)} SET {sets} WHERE ctid = CAST(:ctid AS tid)"), params)
            if apply:
                await trans.commit()
            else:
                await trans.rollback()
        except BaseException:
            if trans.is_active:
                await trans.rollback()
            raise
    return rot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-encrypt stored credentials after rotating SECRET_KEY")
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = parser.parse_args(argv)
    new, old = (os.getenv("SECRET_KEY") or "").strip(), (os.getenv(PREVIOUS_ENV) or "").strip()
    if not new or not old:
        print(f"Set both SECRET_KEY (new) and {PREVIOUS_ENV} (old).")
        return 2
    if new == old:
        print(f"SECRET_KEY and {PREVIOUS_ENV} are identical: nothing to rotate.")
        return 2
    from app.core.database import engine
    rot = asyncio.run(rotate(engine, fernet_for(new), fernet_for(old), apply=args.apply))
    labels = sorted({w for w, _ in rot.counts})
    for label in labels:
        print(f"{label}: rotated={rot.counts[(label, 'rotated')]} already_current={rot.counts[(label, 'already_current')]} "
              f"undecryptable={rot.counts[(label, 'undecryptable')]}")
    total = sum(v for (_, k), v in rot.counts.items() if k == "rotated")
    bad = sum(v for (_, k), v in rot.counts.items() if k == "undecryptable")
    print(f"{'APPLIED' if args.apply else 'DRY RUN (rolled back)'}: rotated={total} undecryptable={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
