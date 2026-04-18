from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _build_fernet() -> Fernet:
    raw_secret = str(os.getenv("SECRET_KEY") or "").strip()
    if not raw_secret:
        raise ValueError("SECRET_KEY is required for secret encryption")
    key_material = hashlib.sha256(raw_secret.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(key_material)
    return Fernet(fernet_key)


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
