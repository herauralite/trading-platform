"""Telegram OIDC: strict nonce, replay protection, and the standard token checks (signature, exp, aud, iss).

Tokens are signed with a throwaway RSA key; the JWKS client is replaced so nothing leaves the machine.
Replay protection runs against a real (SQLite) database through the same SQL used in production."""
import asyncio
import os
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import create_async_engine

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core import database, oidc_replay  # noqa: E402

ISSUER = "https://oauth.telegram.example"
AUDIENCE = "talitrade-client"
NONCE = "n0nce-abcdefghijklmnop"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SigningKey:
    key = KEY.public_key()


class _FakeJWKClient:
    def __init__(self, url):
        self.url = url

    def get_signing_key_from_jwt(self, token):
        return _SigningKey()


def make_token(key=KEY, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "424242", "iat": now, "exp": now + 300, "nonce": NONCE,
              "preferred_username": "alice"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not ...}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def oidc(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "telegram_oidc_enabled", lambda: True)
    monkeypatch.setattr(main, "PyJWKClient", _FakeJWKClient)
    monkeypatch.setattr(main, "TELEGRAM_OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(main, "TELEGRAM_OIDC_AUDIENCE", AUDIENCE)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oidc.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(oidc_replay, "_table_ready", False)
    calls = []

    async def upsert(profile):
        calls.append(profile["id"])

    async def noop(*a, **k):
        return None

    async def accounts(_uid):
        return []
    monkeypatch.setattr(main, "db_upsert_user", upsert)
    monkeypatch.setattr(main, "hydrate_fundingpips_canonical_state", noop)
    monkeypatch.setattr(main, "db_get_user_accounts", accounts)
    yield calls
    asyncio.run(engine.dispose())


def post(body):
    async def _run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/auth/telegram/oidc", json=body)
    return asyncio.run(_run())


def reason(response):
    return response.json().get("reason")


# ── unit: nonce rules ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("expected,claims,code", [
    (None, {"nonce": NONCE}, "oidc_nonce_required"),
    ("", {"nonce": NONCE}, "oidc_nonce_required"),
    ("short", {"nonce": "short"}, "oidc_nonce_malformed"),
    ("bad nonce with spaces!!", {"nonce": "bad nonce with spaces!!"}, "oidc_nonce_malformed"),
    (NONCE, {}, "oidc_nonce_missing"),
    (NONCE, {"nonce": ""}, "oidc_nonce_missing"),
    (NONCE, {"nonce": 12345}, "oidc_nonce_malformed"),
    (NONCE, {"nonce": [NONCE]}, "oidc_nonce_malformed"),
    (NONCE, {"nonce": NONCE + "x"}, "oidc_nonce_mismatch"),
])
def test_nonce_rules(expected, claims, code):
    with pytest.raises(ValueError) as err:
        oidc_replay.validate_nonce_claim(expected, claims)
    assert str(err.value) == code


def test_matching_nonce_passes():
    oidc_replay.validate_nonce_claim(NONCE, {"nonce": NONCE})


# ── endpoint ──────────────────────────────────────────────────────────────────

def test_valid_token_with_matching_nonce_logs_in(oidc):
    response = post({"id_token": make_token(), "nonce": NONCE})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True and oidc == ["424242"]


def test_missing_nonce_claim_is_rejected_when_nonce_expected(oidc):
    response = post({"id_token": make_token(nonce=...), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_nonce_missing"
    assert oidc == []


def test_request_without_nonce_is_rejected(oidc):
    response = post({"id_token": make_token()})
    assert response.status_code == 401 and reason(response) == "oidc_nonce_required"
    response = post({"id_token": make_token(nonce=...)})
    assert response.status_code == 401 and reason(response) == "oidc_nonce_required"
    assert oidc == []


def test_incorrect_nonce_is_rejected(oidc):
    response = post({"id_token": make_token(nonce="other-nonce-0123456789"), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_nonce_mismatch"


def test_malformed_nonce_is_rejected(oidc):
    response = post({"id_token": make_token(nonce="x y"), "nonce": "x y"})
    assert response.status_code == 401 and reason(response) == "oidc_nonce_malformed"


def test_replayed_token_is_rejected(oidc):
    token = make_token()
    assert post({"id_token": token, "nonce": NONCE}).status_code == 200
    replay = post({"id_token": token, "nonce": NONCE})
    assert replay.status_code == 401 and reason(replay) == "oidc_nonce_replayed"
    assert oidc == ["424242"]


def test_same_nonce_for_a_different_subject_is_independent(oidc):
    assert post({"id_token": make_token(), "nonce": NONCE}).status_code == 200
    assert post({"id_token": make_token(sub="777"), "nonce": NONCE}).status_code == 200


def test_expired_token_is_rejected(oidc):
    now = int(time.time())
    response = post({"id_token": make_token(iat=now - 3600, exp=now - 60), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_invalid_token"


def test_invalid_signature_is_rejected(oidc):
    response = post({"id_token": make_token(key=OTHER_KEY), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_invalid_token"


def test_wrong_audience_is_rejected(oidc):
    response = post({"id_token": make_token(aud="someone-else"), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_invalid_token"


def test_wrong_issuer_is_rejected(oidc):
    response = post({"id_token": make_token(iss="https://evil.example"), "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_invalid_token"


def test_unsigned_token_is_rejected(oidc):
    now = int(time.time())
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "1", "iat": now, "exp": now + 300, "nonce": NONCE},
                          None, algorithm="none")
    response = post({"id_token": unsigned, "nonce": NONCE})
    assert response.status_code == 401 and reason(response) == "oidc_invalid_token"


def test_replay_record_window_is_bounded():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    short = oidc_replay.replay_expiry({"exp": int(now.timestamp()) + 5}, now)
    assert short == now + oidc_replay.MIN_REPLAY_WINDOW
    long = oidc_replay.replay_expiry({"exp": int(now.timestamp()) + 10 ** 7}, now)
    assert long == now + oidc_replay.MAX_REPLAY_WINDOW
