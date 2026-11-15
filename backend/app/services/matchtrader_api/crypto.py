"""Authenticated encryption for stored Match-Trader bearer tokens.

AES-256-GCM with:
* key versioning  - TALITRADE_MT_TOKEN_KEYS is a JSON object {"v1": "<base64 32-byte key>", ...}
                    and TALITRADE_MT_TOKEN_KEY_ACTIVE names the version used for new writes;
* a random 96-bit nonce per encryption;
* associated data binding the ciphertext to its owner, connection and external identity, so
  a ciphertext copied onto another row fails to decrypt.

Keys live only in the deployment environment / secret manager. They are deliberately separate
from SECRET_KEY (session signing) so the two can be rotated independently.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_ENV = "TALITRADE_MT_TOKEN_KEYS"
ACTIVE_ENV = "TALITRADE_MT_TOKEN_KEY_ACTIVE"


class TokenCryptoError(Exception):
    """Raised for any key/ciphertext problem. Never carries key or token material."""


def _keys() -> dict[str, bytes]:
    raw = str(os.getenv(KEYS_ENV) or "").strip()
    if not raw:
        raise TokenCryptoError("token encryption keys are not configured")
    try:
        parsed = json.loads(raw)
        keys = {str(version): base64.b64decode(str(value), validate=True) for version, value in parsed.items()}
    except Exception:
        raise TokenCryptoError("token encryption keys are malformed") from None
    if not keys or any(len(key) != 32 for key in keys.values()) or any("." in v or not v for v in keys):
        raise TokenCryptoError("token encryption keys must be 32-byte base64 values with simple version names")
    return keys


def active_key_version() -> str:
    keys = _keys()
    active = str(os.getenv(ACTIVE_ENV) or "").strip()
    if active not in keys:
        raise TokenCryptoError("active token key version is not configured")
    return active


def is_configured() -> bool:
    try:
        active_key_version()
        return True
    except TokenCryptoError:
        return False


def build_associated_data(*, user_id: str, connection_id: int, platform_host: str, external_login: str) -> bytes:
    return f"mtapi|{user_id}|{int(connection_id)}|{platform_host}|{external_login}".encode("utf-8")


def encrypt_token(token: str, associated_data: bytes) -> str:
    if not token:
        raise TokenCryptoError("empty token")
    version = active_key_version()
    nonce = os.urandom(12)
    ciphertext = AESGCM(_keys()[version]).encrypt(nonce, token.encode("utf-8"), associated_data)
    return ".".join((version, base64.b64encode(nonce).decode(), base64.b64encode(ciphertext).decode()))


def decrypt_token(blob: str, associated_data: bytes) -> str:
    try:
        version, nonce_b64, ciphertext_b64 = str(blob).split(".")
        key = _keys()[version]
        return AESGCM(key).decrypt(base64.b64decode(nonce_b64), base64.b64decode(ciphertext_b64), associated_data).decode("utf-8")
    except TokenCryptoError:
        raise
    except (InvalidTag, KeyError, ValueError):
        raise TokenCryptoError("stored token could not be decrypted") from None


def key_version_of(blob: str) -> str:
    return str(blob).split(".", 1)[0]


def mask_token(token: str) -> str:
    """Display hint only. Never enough to reconstruct or identify the token."""
    return f"...{token[-4:]}" if len(token) >= 24 else "..."
