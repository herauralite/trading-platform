"""SECRET_KEY rotation: stored credentials survive it, sessions do not."""
import asyncio
import json
import os

import pytest
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import auth_session  # noqa: E402
from app.services import secret_crypto  # noqa: E402
from app.tools import reencrypt_secrets  # noqa: E402

OLD, NEW = "old-key-" + "a" * 24, "new-key-" + "b" * 24


@pytest.fixture
def keys(monkeypatch):
    def set_keys(current, previous=None):
        monkeypatch.setenv("SECRET_KEY", current)
        if previous:
            monkeypatch.setenv(secret_crypto.PREVIOUS_ENV, previous)
        else:
            monkeypatch.delenv(secret_crypto.PREVIOUS_ENV, raising=False)
    return set_keys


def test_old_ciphertext_still_decrypts_during_rotation_and_new_writes_use_the_new_key(keys):
    keys(OLD)
    stored = secret_crypto.encrypt_secret("fp-password")
    keys(NEW)
    with pytest.raises(InvalidToken):                      # what rotating without SECRET_KEY_PREVIOUS does today
        secret_crypto.decrypt_secret(stored)
    keys(NEW, previous=OLD)
    assert secret_crypto.decrypt_secret(stored) == "fp-password"
    fresh = secret_crypto.encrypt_secret("fp-password")
    secret_crypto.fernet_for(NEW).decrypt(fresh.encode())  # written under the new key only
    with pytest.raises(InvalidToken):
        secret_crypto.fernet_for(OLD).decrypt(fresh.encode())


def test_sessions_signed_with_the_old_key_are_rejected_even_with_previous_key_set(keys):
    keys(OLD)
    token = auth_session.create_session_token("42")
    keys(NEW, previous=OLD)
    with pytest.raises(HTTPException) as err:
        auth_session.decode_session_token(token)
    assert err.value.status_code == 401


def test_rotator_rewrites_only_tokens_of_the_previous_key_including_nested_json():
    old, new = secret_crypto.fernet_for(OLD), secret_crypto.fernet_for(NEW)
    rot = reencrypt_secrets.Rotator(new, old)
    doc = {"platform_credentials": {"password": old.encrypt(b"p").decode(), "login": "12345"},
           "tokens": [old.encrypt(b"t").decode(), new.encrypt(b"n").decode(), "gAAAAAnot-a-token"]}
    out = rot.walk("t.c", json.loads(json.dumps(doc)))
    assert new.decrypt(out["platform_credentials"]["password"].encode()) == b"p"
    assert new.decrypt(out["tokens"][0].encode()) == b"t"
    assert out["tokens"][1] == doc["tokens"][1] and out["tokens"][2] == doc["tokens"][2]
    assert out["platform_credentials"]["login"] == "12345"
    assert rot.counts[("t.c", "rotated")] == 2 and rot.counts[("t.c", "already_current")] == 1
    assert rot.counts[("t.c", "undecryptable")] == 1


def test_tool_refuses_to_run_without_both_keys_and_never_prints_them(keys, capsys):
    keys(NEW)
    assert reencrypt_secrets.main([]) == 2
    keys(NEW, previous=NEW)
    assert reencrypt_secrets.main([]) == 2
    assert NEW not in capsys.readouterr().out


URL = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
_db = URL.rsplit("/", 1)[-1].split("?")[0].lower()
needs_db = pytest.mark.skipif(not URL or not ("test" in _db or "disposable" in _db),
                              reason="set TALITRADE_TEST_DATABASE_URL to a disposable test database")


@needs_db
def test_tool_on_postgres_dry_run_changes_nothing_apply_rotates_rerun_is_a_no_op():
    old, new = secret_crypto.fernet_for(OLD), secret_crypto.fernet_for(NEW)

    async def scenario():
        engine = create_async_engine(URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS rot_test_creds"))
                await conn.execute(text("CREATE TABLE rot_test_creds (id INT PRIMARY KEY, enc_password TEXT, "
                                        "secret_config JSONB, note VARCHAR(50))"))
                await conn.execute(text("INSERT INTO rot_test_creds VALUES (1, :a, CAST(:j AS jsonb), 'plain'), "
                                        "(2, NULL, '{}'::jsonb, NULL)"),
                                   {"a": old.encrypt(b"pw1").decode(),
                                    "j": json.dumps({"bridge_api_key": old.encrypt(b"k1").decode(), "n": 1})})

            async def snapshot():
                async with engine.connect() as conn:
                    return (await conn.execute(text("SELECT enc_password, secret_config::text FROM rot_test_creds WHERE id=1"))).one()

            before = await snapshot()
            dry = await reencrypt_secrets.rotate(engine, new, old, apply=False)
            assert dry.counts[("rot_test_creds.enc_password", "rotated")] == 1
            assert dry.counts[("rot_test_creds.secret_config", "rotated")] == 1
            assert await snapshot() == before                                   # rolled back

            await reencrypt_secrets.rotate(engine, new, old, apply=True)
            enc, cfg = await snapshot()
            assert new.decrypt(enc.encode()) == b"pw1"
            assert new.decrypt(json.loads(cfg)["bridge_api_key"].encode()) == b"k1" and json.loads(cfg)["n"] == 1

            again = await reencrypt_secrets.rotate(engine, new, old, apply=True)
            assert again.counts[("rot_test_creds.enc_password", "rotated")] == 0
            assert again.counts[("rot_test_creds.enc_password", "already_current")] == 1
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE rot_test_creds"))
        finally:
            await engine.dispose()
    asyncio.run(scenario())
