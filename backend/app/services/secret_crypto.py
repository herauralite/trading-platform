"""Encryption of stored connector credentials, keyed from SECRET_KEY.

Rotation: set the new value as SECRET_KEY and the old one as SECRET_KEY_PREVIOUS. New values are always
encrypted with SECRET_KEY; existing ciphertexts still decrypt with SECRET_KEY_PREVIOUS until
`python -m app.tools.reencrypt_secrets --apply` has re-encrypted them, after which SECRET_KEY_PREVIOUS is
removed. (Session tokens are signed with SECRET_KEY only and are deliberately NOT accepted under the
previous key: rotating SECRET_KEY must invalidate every session, including any forged with a leaked key.)
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, MultiFernet

PREVIOUS_ENV = "SECRET_KEY_PREVIOUS"


def fernet_for(raw_secret: str) -> Fernet:
    raw_secret = str(raw_secret or "").strip()
    if not raw_secret:
        raise ValueError("SECRET_KEY is required for secret encryption")
    key_material = hashlib.sha256(raw_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def _current() -> Fernet:
    return fernet_for(os.getenv("SECRET_KEY") or "")


def _previous() -> Fernet | None:
    raw = str(os.getenv(PREVIOUS_ENV) or "").strip()
    return fernet_for(raw) if raw else None


def _build_fernet() -> MultiFernet:
    """Encrypts with SECRET_KEY; decrypts with SECRET_KEY, then SECRET_KEY_PREVIOUS if set."""
    keys = [_current()]
    previous = _previous()
    if previous is not None:
        keys.append(previous)
    return MultiFernet(keys)


def encrypt_secret(value: str) -> str:
    plaintext = str(value or "").strip()
    if not plaintext:
        raise ValueError("secret value is required")
    return _build_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    token = str(ciphertext or "").strip()
    if not token:
        raise ValueError("ciphertext is required")
    return _build_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
