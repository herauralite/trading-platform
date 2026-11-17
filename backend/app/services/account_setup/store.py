"""Persistence for account setup profiles.

One row per (trader, linked account). The account is identified the way the Match-Trader
integration identifies it (platform host + login), so a profile survives a token rotation or a
disconnect/reconnect of the same account by the same trader. Every read and write first resolves
the connection *owned by the signed-in user*; a profile is never reachable through someone else's
connection id.

Writes are optimistic: the client sends the version it last saw. A different version means
another tab or device saved in between, and the write is refused (``Conflict``) instead of
overwriting newer answers. Each save carries a client ``save_id``; repeating the same save (a
retry after a lost response) returns the stored result instead of failing as a conflict.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import text

from app.services.matchtrader_api.read_models import mask_login

PROVIDER = "matchtrader_api"

DDL = [
    """CREATE TABLE IF NOT EXISTS account_setup_profiles (
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        platform_host TEXT NOT NULL,
        external_login TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_save_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (user_id, provider, platform_host, external_login)
    )""",
]
DROP_ORDER = ["account_setup_profiles"]

_OWNED_CONNECTION = """SELECT id, user_id, platform_host, external_login, broker_name, account_currency, account_type, account_created_at
                       FROM mtapi_connections WHERE id = :id AND user_id = :user AND status <> 'disconnected'"""


class NotFound(Exception):
    """No connection with this id is owned by this user (same answer whether it exists or not)."""


class Conflict(Exception):
    def __init__(self, version: int, data: dict, updated_at: datetime | None):
        super().__init__("profile changed since base_version")
        self.version, self.data, self.updated_at = version, data, updated_at


def _default_engine():
    from app.core.database import engine

    return engine


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _money(value):
    return None if value is None else format(value, "f")


class SetupStore:
    def __init__(self, engine_getter: Callable[[], Any] = _default_engine):
        self._engine_getter = engine_getter

    @property
    def engine(self):
        return self._engine_getter()

    async def ensure_schema(self) -> None:
        async with self.engine.begin() as conn:
            for statement in DDL:
                await conn.execute(text(statement))

    async def read(self, connection_id: int, user_id: str) -> dict:
        async with self.engine.connect() as conn:
            c = (await conn.execute(text(_OWNED_CONNECTION), {"id": connection_id, "user": user_id})).mappings().first()
            if not c:
                raise NotFound()
            row = (await conn.execute(text("""SELECT version, data, updated_at FROM account_setup_profiles
                       WHERE user_id = :user AND provider = :p AND platform_host = :h AND external_login = :l"""),
                       {"user": user_id, "p": PROVIDER, "h": c["platform_host"], "l": c["external_login"]})).mappings().first()
            return {"connection_id": c["id"], "version": row["version"] if row else 0, "profile": dict(row["data"]) if row else None,
                    "updated_at": _iso(row["updated_at"]) if row else None, "detected": await self._detected(conn, c)}

    async def list_owned(self, user_id: str) -> list[dict]:
        """Every live connection of this user with its profile (or none). One query."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT c.id, p.version, p.data, p.updated_at FROM mtapi_connections c
                LEFT JOIN account_setup_profiles p ON p.user_id = c.user_id AND p.provider = :p
                     AND p.platform_host = c.platform_host AND p.external_login = c.external_login
                WHERE c.user_id = :user AND c.status <> 'disconnected' ORDER BY c.id"""), {"user": user_id, "p": PROVIDER})).mappings().all()
        return [{"connection_id": r["id"], "version": r["version"] or 0, "profile": dict(r["data"]) if r["data"] is not None else None,
                 "updated_at": _iso(r["updated_at"])} for r in rows]

    async def save(self, connection_id: int, user_id: str, *, base_version: int, save_id: str, data: dict) -> dict:
        async with self.engine.begin() as conn:
            c = (await conn.execute(text(_OWNED_CONNECTION + " FOR UPDATE"), {"id": connection_id, "user": user_id})).mappings().first()
            if not c:
                raise NotFound()
            key = {"user": user_id, "p": PROVIDER, "h": c["platform_host"], "l": c["external_login"]}
            await conn.execute(text("""INSERT INTO account_setup_profiles (user_id, provider, platform_host, external_login)
                                       VALUES (:user, :p, :h, :l) ON CONFLICT DO NOTHING"""), key)
            row = (await conn.execute(text("""SELECT version, data, last_save_id, updated_at FROM account_setup_profiles
                       WHERE user_id = :user AND provider = :p AND platform_host = :h AND external_login = :l FOR UPDATE"""), key)).mappings().first()
            if row["last_save_id"] == save_id:            # the same save again (retry after a lost answer)
                return {"version": row["version"], "profile": dict(row["data"]), "updated_at": _iso(row["updated_at"]), "replayed": True}
            if row["version"] != base_version:
                raise Conflict(row["version"], dict(row["data"]) if row["version"] else {}, row["updated_at"])
            saved = (await conn.execute(text("""UPDATE account_setup_profiles
                       SET data = CAST(:data AS jsonb), version = version + 1, last_save_id = :sid, updated_at = NOW()
                       WHERE user_id = :user AND provider = :p AND platform_host = :h AND external_login = :l
                       RETURNING version, data, updated_at"""), {**key, "data": json.dumps(data), "sid": save_id})).mappings().first()
            return {"version": saved["version"], "profile": dict(saved["data"]), "updated_at": _iso(saved["updated_at"]), "replayed": False}

    @staticmethod
    async def _detected(conn, c) -> dict:
        """What the broker actually reported, labelled field by field. Nothing is inferred: the
        Match-Trader account endpoint has no starting size, evaluation phase or program rules,
        and its accountType only says REAL or DEMO."""
        s = (await conn.execute(text("""SELECT a.balance, a.equity, a.fetched_at FROM mtapi_account_summary a
                     JOIN mtapi_sync_state st ON st.connection_id = a.connection_id AND st.resource = 'summary' AND st.last_success_at IS NOT NULL
                     WHERE a.connection_id = :id"""), {"id": c["id"]})).mappings().first()
        return {
            "source": "Match-Trader Trader API (GET /v1/platform-details, /v1/account, /v1/account-summary)",
            "broker": c["broker_name"] or None,
            "masked_login": mask_login(c["external_login"]),
            "currency": c["account_currency"],
            "reported_account_type": c["account_type"],          # REAL or DEMO: not evaluation vs funded
            "account_created_at": _iso(c["account_created_at"]),
            "balance": _money(s["balance"]) if s else None,
            "equity": _money(s["equity"]) if s else None,
            "observed_at": _iso(s["fetched_at"]) if s else None,
            "not_supplied": ["starting_size", "evaluation_or_funded", "evaluation_phase", "profit_target", "loss_limits", "payout_rules"],
        }
