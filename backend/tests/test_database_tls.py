"""Database TLS modes. The default stays what it was; verification is opt-in and never falls back."""
import os
import ssl

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import database  # noqa: E402


def test_default_mode_is_unchanged_and_reported_as_unverified(monkeypatch):
    monkeypatch.delenv("TALITRADE_DB_SSL_MODE", raising=False)
    monkeypatch.delenv("TALITRADE_DB_SSL_ROOT_CERT", raising=False)
    context = database.build_ssl_context()
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False


def test_verify_modes_require_a_valid_chain_and_verify_full_also_checks_the_host_name(monkeypatch):
    import certifi
    monkeypatch.setenv("TALITRADE_DB_SSL_MODE", "verify-full")
    monkeypatch.setenv("TALITRADE_DB_SSL_ROOT_CERT", certifi.where())
    full = database.build_ssl_context()
    assert full.verify_mode == ssl.CERT_REQUIRED and full.check_hostname is True
    ca_only = database.build_ssl_context("verify-ca", certifi.where())
    assert ca_only.verify_mode == ssl.CERT_REQUIRED and ca_only.check_hostname is False
    system_store = database.build_ssl_context(" Verify-Full ", "")
    assert system_store.verify_mode == ssl.CERT_REQUIRED and system_store.check_hostname is True


def test_misconfiguration_stops_startup_instead_of_falling_back_to_unverified(tmp_path):
    with pytest.raises(ValueError):
        database.build_ssl_context("verify")                      # unknown mode
    with pytest.raises(ValueError):
        database.build_ssl_context("disable")                     # there is no unencrypted mode
    with pytest.raises(OSError):
        database.build_ssl_context("verify-full", str(tmp_path / "missing.pem"))
    not_a_cert = tmp_path / "bad.pem"
    not_a_cert.write_text("this is not a certificate")
    with pytest.raises(ssl.SSLError):
        database.build_ssl_context("verify-ca", str(not_a_cert))
    with pytest.raises(ValueError):
        database.build_ssl_context("require", "/some/ca.pem")     # a CA with verification off is a mistake, not a preference
