"""Authentication and account-ownership checks for trading ingestion and private reads.

Principals
----------
Every protected route resolves the caller to a ``Principal`` on the server:

* a normal user session (the same bearer token the dashboard uses), or
* an *extension token* (``tti_...``): a revocable, per-user credential that the browser
  extension uses for ingestion. It is minted only for an authenticated session, stored
  in the database as a SHA-256 hash, expires, and is accepted ONLY by routes that depend
  on ``require_ingest_principal``. It is never a general API credential.

A user id, Telegram id or account id that merely appears in a request is never trusted.

Ownership
---------
``trades`` rows are keyed by the external account id, so an external account id may be
owned by one user only. ``authorize_account_write`` lets a user write to an account they
already own, or claim a brand-new one nobody owns (an unowned account that already has
trade history cannot be claimed); ``authorize_account_read`` requires ownership.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from sqlalchemy import text

from app.core.auth_session import decode_session_token, get_bearer_token

logger = logging.getLogger("app.ingest_auth")

EXTENSION_TOKEN_PREFIX = "tti_"
EXTENSION_TOKEN_SCOPE = "extension_ingest"
EXTENSION_TOKEN_TTL_DAYS = 90
MAX_ACTIVE_EXTENSION_TOKENS = 5
_LAST_USED_REFRESH = timedelta(minutes=10)


@dataclass(frozen=True)
class Principal:
    user_id: str
    kind: str  # "session" | "extension_token"


def _engine():
    from app.core.database import engine

    return engine


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ─── Extension tokens ─────────────────────────────────────────────────────────
async def ensure_integration_tokens_table() -> None:
    async with _engine().begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS integration_tokens (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'extension_ingest',
                token_hash TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                last_used_at TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_integration_tokens_user ON integration_tokens (user_id)"))


async def mint_extension_token(user_id: str) -> dict:
    """Create a new extension token for ``user_id``. The raw token is returned once and never stored."""
    token = EXTENSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=EXTENSION_TOKEN_TTL_DAYS)
    async with _engine().begin() as conn:
        await conn.execute(
            text("INSERT INTO integration_tokens (user_id, scope, token_hash, expires_at) VALUES (:u, :s, :h, :e)"),
            {"u": user_id, "s": EXTENSION_TOKEN_SCOPE, "h": _hash_token(token), "e": expires_at},
        )
        # Keep only the newest few active tokens per user (one per browser is typical).
        await conn.execute(text("""
            UPDATE integration_tokens SET revoked_at = NOW()
            WHERE user_id = :u AND scope = :s AND revoked_at IS NULL
              AND id NOT IN (
                  SELECT id FROM integration_tokens
                  WHERE user_id = :u AND scope = :s AND revoked_at IS NULL
                  ORDER BY id DESC LIMIT :keep
              )
        """), {"u": user_id, "s": EXTENSION_TOKEN_SCOPE, "keep": MAX_ACTIVE_EXTENSION_TOKENS})
    return {"token": token, "expires_at": expires_at.isoformat()}


async def revoke_extension_tokens(user_id: str) -> int:
    async with _engine().begin() as conn:
        result = await conn.execute(
            text("UPDATE integration_tokens SET revoked_at = NOW() WHERE user_id = :u AND scope = :s AND revoked_at IS NULL"),
            {"u": user_id, "s": EXTENSION_TOKEN_SCOPE},
        )
    return int(result.rowcount or 0)


async def resolve_extension_token(token: str) -> str | None:
    """Return the owning user id for a live extension token, else None."""
    if not token.startswith(EXTENSION_TOKEN_PREFIX) or len(token) > 200:
        return None
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    async with _engine().begin() as conn:
        row = (await conn.execute(text("""
            SELECT id, user_id, last_used_at FROM integration_tokens
            WHERE token_hash = :h AND scope = :s AND revoked_at IS NULL AND expires_at > NOW()
        """), {"h": token_hash, "s": EXTENSION_TOKEN_SCOPE})).mappings().first()
        if not row:
            return None
        last_used = row["last_used_at"]
        if last_used is None or now - last_used > _LAST_USED_REFRESH:
            await conn.execute(text("UPDATE integration_tokens SET last_used_at = NOW() WHERE id = :id"), {"id": row["id"]})
    return str(row["user_id"])


# ─── Principals ───────────────────────────────────────────────────────────────
async def require_session_principal(token: str | None = Depends(get_bearer_token)) -> Principal:
    """User session only. Extension tokens are rejected here."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing authenticated session")
    if token.startswith(EXTENSION_TOKEN_PREFIX):
        raise HTTPException(status_code=401, detail="This route requires a user session")
    return Principal(user_id=str(decode_session_token(token)["sub"]), kind="session")


async def require_ingest_principal(token: str | None = Depends(get_bearer_token)) -> Principal:
    """User session or a live extension token."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing authenticated session")
    if token.startswith(EXTENSION_TOKEN_PREFIX):
        user_id = await resolve_extension_token(token)
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid or revoked integration token")
        return Principal(user_id=user_id, kind="extension_token")
    return Principal(user_id=str(decode_session_token(token)["sub"]), kind="session")


async def optional_session_principal(token: str | None = Depends(get_bearer_token)) -> Principal | None:
    """For public routes that personalise when a valid session happens to be present."""
    if not token or token.startswith(EXTENSION_TOKEN_PREFIX):
        return None
    try:
        return Principal(user_id=str(decode_session_token(token)["sub"]), kind="session")
    except HTTPException:
        return None


# ─── Account ownership ────────────────────────────────────────────────────────
def _clean_account_id(external_account_id) -> str:
    account_id = str(external_account_id or "").strip()
    if not account_id or len(account_id) > 128:
        raise HTTPException(status_code=422, detail="A valid account id is required")
    return account_id


async def get_account_owners(external_account_id: str) -> set[str]:
    async with _engine().connect() as conn:
        result = await conn.execute(text("""
            SELECT user_id AS owner FROM trading_accounts
            WHERE external_account_id = :a AND user_id IS NOT NULL
            UNION
            SELECT telegram_user_id AS owner FROM prop_accounts
            WHERE account_id = :a AND is_active = TRUE
        """), {"a": external_account_id})
        return {str(r["owner"]) for r in result.mappings().all() if r["owner"]}


async def get_owned_account_ids(user_id: str) -> list[str]:
    async with _engine().connect() as conn:
        result = await conn.execute(text("""
            SELECT external_account_id AS account_id FROM trading_accounts WHERE user_id = :u
            UNION
            SELECT account_id FROM prop_accounts WHERE telegram_user_id = :u AND is_active = TRUE
            ORDER BY account_id
        """), {"u": user_id})
        return [str(r["account_id"]) for r in result.mappings().all() if r["account_id"]]


async def account_has_trades(external_account_id: str) -> bool:
    async with _engine().connect() as conn:
        row = (await conn.execute(text("SELECT 1 FROM trades WHERE account_id = :a LIMIT 1"), {"a": external_account_id})).first()
    return row is not None


async def authorize_account_write(user_id: str, external_account_id) -> str:
    """Allow writing to an account the user owns, or claiming a new one that nobody owns.

    An account that already holds trade history but has no owner cannot be claimed through
    the API: otherwise anyone could adopt it and read that history. Such accounts must be
    assigned to their user by an operator.
    """
    account_id = _clean_account_id(external_account_id)
    owners = await get_account_owners(account_id)
    if owners:
        if str(user_id) not in owners:
            logger.warning("ownership denied: write user=%s account=%s", user_id, account_id)
            raise HTTPException(status_code=403, detail="This account is linked to a different user")
        return account_id
    if await account_has_trades(account_id):
        logger.warning("ownership denied: unowned account with existing history user=%s account=%s", user_id, account_id)
        raise HTTPException(status_code=403, detail="This account already has history and must be linked by support")
    return account_id


async def authorize_account_read(user_id: str, external_account_id) -> str:
    """Allow reading only an account the user owns."""
    account_id = _clean_account_id(external_account_id)
    owners = await get_account_owners(account_id)
    if str(user_id) not in owners:
        logger.warning("ownership denied: read user=%s account=%s", user_id, account_id)
        raise HTTPException(status_code=403, detail="This account is not linked to your user")
    return account_id
