"""Telegram OIDC nonce validation and one-time use.

The browser generates the nonce, sends it to Telegram in the authorize request, and posts it back with the
id_token. The server therefore requires:

* a nonce in the request (no nonce -> reject; there is no "nonce optional" path),
* a well-formed nonce (URL-safe characters, bounded length),
* a `nonce` claim in the signed id_token that is a string and equals the request nonce (constant time),
* that the (issuer, subject, nonce) triple has not been accepted before (replay protection). The record is
  kept in the database so it holds across workers and restarts, and expires with the token.

Only a SHA-256 of the triple is stored; nonces and tokens are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

NONCE_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
# Keep replay records at least this long even if a token claims a shorter life.
MIN_REPLAY_WINDOW = timedelta(minutes=10)
MAX_REPLAY_WINDOW = timedelta(days=2)

_table_ready = False


def validate_nonce_claim(expected_nonce: str | None, claims: dict[str, Any]) -> None:
    """Raise ValueError with a stable reason code unless the token's nonce matches the request nonce."""
    if not isinstance(expected_nonce, str) or not expected_nonce:
        raise ValueError("oidc_nonce_required")
    if not NONCE_PATTERN.fullmatch(expected_nonce):
        raise ValueError("oidc_nonce_malformed")
    claim = claims.get("nonce")
    if claim is None or claim == "":
        raise ValueError("oidc_nonce_missing")
    if not isinstance(claim, str):
        raise ValueError("oidc_nonce_malformed")
    if not hmac.compare_digest(claim.encode(), expected_nonce.encode()):
        raise ValueError("oidc_nonce_mismatch")


def replay_key(claims: dict[str, Any], nonce: str) -> str:
    material = "\x1f".join((str(claims.get("iss") or ""), str(claims.get("sub") or ""), nonce))
    return hashlib.sha256(material.encode()).hexdigest()


def replay_expiry(claims: dict[str, Any], now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    try:
        exp = datetime.fromtimestamp(int(claims.get("exp")), tz=timezone.utc)
    except Exception:
        exp = now
    return min(max(exp, now + MIN_REPLAY_WINDOW), now + MAX_REPLAY_WINDOW)


async def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS oidc_consumed_nonces (
            nonce_hash TEXT PRIMARY KEY,
            expires_at TIMESTAMPTZ NOT NULL
        )
    """))
    _table_ready = True


async def consume_nonce(claims: dict[str, Any], nonce: str, engine=None) -> None:
    """Record the nonce as used. Raise ValueError('oidc_nonce_replayed') if it was already used."""
    if engine is None:
        from app.core.database import engine as default_engine
        engine = default_engine
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await _ensure_table(conn)
        await conn.execute(text("DELETE FROM oidc_consumed_nonces WHERE expires_at < :now"), {"now": now})
        inserted = await conn.execute(
            text("INSERT INTO oidc_consumed_nonces (nonce_hash, expires_at) VALUES (:h, :e) "
                 "ON CONFLICT (nonce_hash) DO NOTHING RETURNING nonce_hash"),
            {"h": replay_key(claims, nonce), "e": replay_expiry(claims, now)},
        )
        if inserted.first() is None:
            logger.warning("Telegram OIDC login rejected: nonce already used")
            raise ValueError("oidc_nonce_replayed")
