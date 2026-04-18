from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import settings


def _fernet_key_from_secret(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    secret = str(settings.SECRET_KEY or "").strip()
    if not secret:
        raise RuntimeError("SECRET_KEY is required for connector secret encryption")
    return Fernet(_fernet_key_from_secret(secret))


def encrypt_secret_value(raw_value: str) -> str:
    return _fernet().encrypt(raw_value.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
