"""Database TLS: verify-full is the default, unverified TLS is refused in production, nothing falls back.

Includes real TLS handshakes against a local server with a throwaway self-signed CA, so the contexts are
proven to accept the right server and reject a wrong host name or an untrusted certificate."""
import contextlib
import datetime
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import database  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TALITRADE_DB_SSL_MODE", "TALITRADE_DB_SSL_ROOT_CERT", "TALITRADE_DB_SSL_ROOT_CERT_PEM",
                "TALITRADE_ENV", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)


# ── configuration ─────────────────────────────────────────────────────────────

def test_default_is_verify_full_with_hostname_check():
    context = database.build_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_default_trust_store_is_certifi_bundle(monkeypatch):
    import certifi
    seen = {}
    real = ssl.create_default_context

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(database.ssl, "create_default_context", spy)
    database.build_ssl_context()
    assert seen.get("cafile") == certifi.where()


def test_verify_ca_checks_chain_without_hostname():
    import certifi
    context = database.build_ssl_context("verify-ca", certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is False


def test_require_is_allowed_only_outside_production():
    context = database.build_ssl_context("require", production=False)
    assert context.verify_mode == ssl.CERT_NONE
    with pytest.raises(RuntimeError):
        database.build_ssl_context("require", production=True)


@pytest.mark.parametrize("env", [{"TALITRADE_ENV": "production"}, {"TALITRADE_ENV": "PROD"},
                                 {"RAILWAY_ENVIRONMENT_NAME": "production"}, {"RAILWAY_ENVIRONMENT": "staging"}])
def test_production_detection(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert database.is_production_environment() is True
    monkeypatch.setenv("TALITRADE_DB_SSL_MODE", "require")
    with pytest.raises(RuntimeError):
        database.build_ssl_context()


def test_not_production_by_default():
    assert database.is_production_environment() is False


def test_misconfiguration_stops_startup_instead_of_falling_back(tmp_path):
    with pytest.raises(ValueError):
        database.build_ssl_context("verify")                       # unknown mode
    with pytest.raises(ValueError):
        database.build_ssl_context("disable")                      # no unencrypted mode exists
    with pytest.raises(OSError):
        database.build_ssl_context("verify-full", str(tmp_path / "missing.pem"))
    not_a_cert = tmp_path / "bad.pem"
    not_a_cert.write_text("this is not a certificate")
    with pytest.raises(ssl.SSLError):
        database.build_ssl_context("verify-ca", str(not_a_cert))
    with pytest.raises(ssl.SSLError):
        database.build_ssl_context("verify-full", root_cert_pem="-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n")
    with pytest.raises(ValueError):
        database.build_ssl_context("require", "/some/ca.pem", production=False)
    with pytest.raises(ValueError):
        database.build_ssl_context("verify-full", "/a.pem", "pem-text")


def _import_database(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TALITRADE_DB_SSL", "RAILWAY_", "TALITRADE_ENV"))}
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", "import app.core.database"], cwd=BACKEND_DIR, env=env,
                          capture_output=True, text=True, timeout=60)


def test_import_fails_closed_in_production_with_require():
    result = _import_database({"DATABASE_URL": "postgresql+asyncpg://u:pw@db.example/x",
                               "RAILWAY_ENVIRONMENT_NAME": "production", "TALITRADE_DB_SSL_MODE": "require"})
    assert result.returncode != 0 and "not allowed in production" in result.stderr


def test_import_fails_clearly_without_database_url():
    result = _import_database({"DATABASE_URL": ""})
    assert result.returncode != 0 and "DATABASE_URL is not set" in result.stderr


def test_import_succeeds_in_production_with_default_verification():
    result = _import_database({"DATABASE_URL": "postgresql+asyncpg://u:pw@db.example/x",
                               "RAILWAY_ENVIRONMENT_NAME": "production"})
    assert result.returncode == 0, result.stderr


# ── real handshakes ───────────────────────────────────────────────────────────

def _make_ca_and_server_cert(tmp_path, hostname="localhost", ip_san=True):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TaliTrade Test CA")])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]
                                                       + ([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))] if ip_san else [])),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / "ca.pem").write_text(ca_pem)
    (tmp_path / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    return tmp_path / "ca.pem", ca_pem


@pytest.fixture
def tls_server(tmp_path):
    ca_path, ca_pem = _make_ca_and_server_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            try:
                conn.settimeout(5)
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield port, str(ca_path), ca_pem
    stop.set()
    thread.join(timeout=2)
    sock.close()


def _handshake(context, port, server_hostname):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=server_hostname) as tls:
            tls.sendall(b"x")
            return tls.version()


def test_verify_full_accepts_trusted_cert_with_matching_hostname(tls_server):
    port, ca_path, ca_pem = tls_server
    assert _handshake(database.build_ssl_context("verify-full", ca_path), port, "localhost")
    assert _handshake(database.build_ssl_context("verify-full", root_cert_pem=ca_pem), port, "localhost")


def test_verify_full_rejects_hostname_mismatch(tls_server):
    port, ca_path, _ = tls_server
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(database.build_ssl_context("verify-full", ca_path), port, "attacker.example")


def test_verify_ca_accepts_mismatched_hostname(tls_server):
    port, ca_path, _ = tls_server
    assert _handshake(database.build_ssl_context("verify-ca", ca_path), port, "other.example")


def test_default_trust_store_rejects_untrusted_certificate(tls_server):
    port, _, _ = tls_server
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(database.build_ssl_context(), port, "localhost")


# ── the real engine path (SQLAlchemy + asyncpg), not just the SSLContext object ──
# A fake PostgreSQL endpoint: answers the SSLRequest with 'S', then does a TLS handshake with a certificate
# issued to "db.test" only (no IP SAN). The app engine connects to 127.0.0.1, so verify-full must refuse the
# certificate while verify-ca (same CA, no host name check) must complete the handshake.

@contextlib.contextmanager
def _fake_postgres_tls_server(tmp_path, hostname, ip_san):
    ca_path, _ = _make_ca_and_server_cert(tmp_path, hostname=hostname, ip_san=ip_san)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    stop, handshakes = threading.Event(), []

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            try:
                conn.settimeout(5)
                if len(conn.recv(8)) == 8:          # SSLRequest: int32 length + int32 code 80877103
                    conn.sendall(b"S")
                    with server_ctx.wrap_socket(conn, server_side=True):
                        handshakes.append(True)     # completed: the client accepted our certificate
            except (ssl.SSLError, OSError):
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1], str(ca_path), handshakes
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()


@pytest.fixture
def fake_postgres_tls(tmp_path):
    with _fake_postgres_tls_server(tmp_path, hostname="db.test", ip_san=False) as server:
        yield server


@pytest.fixture
def fake_postgres_tls_matching_host(tmp_path):
    # Certificate names 127.0.0.1 (IP SAN), i.e. exactly the host the engine connects to.
    with _fake_postgres_tls_server(tmp_path, hostname="localhost", ip_san=True) as server:
        yield server


_ENGINE_CONNECT = """
import asyncio
from app.core import database

async def main():
    try:
        async with database.engine.connect():
            pass
    except BaseException as exc:
        chain, e = [], exc
        while e is not None and len(chain) < 10:
            chain.append(type(e).__name__)
            e = e.__cause__ or e.__context__
        print("CHAIN=" + ",".join(chain))

asyncio.run(main())
"""


def _engine_connect(port, env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TALITRADE_DB_SSL", "RAILWAY_", "TALITRADE_ENV"))}
    env.update({"DATABASE_URL": f"postgresql+asyncpg://u:pw@127.0.0.1:{port}/db", **env_overrides})
    return subprocess.run([sys.executable, "-c", _ENGINE_CONNECT], cwd=BACKEND_DIR, env=env,
                          capture_output=True, text=True, timeout=60)


def test_engine_verify_full_refuses_certificate_for_another_host(fake_postgres_tls):
    port, ca_path, handshakes = fake_postgres_tls
    result = _engine_connect(port, {"TALITRADE_DB_SSL_ROOT_CERT": ca_path, "RAILWAY_ENVIRONMENT_NAME": "production"})
    assert "SSLCertVerificationError" in result.stdout, result.stdout + result.stderr
    assert not handshakes


def test_engine_default_trust_store_refuses_private_ca(fake_postgres_tls):
    port, _, handshakes = fake_postgres_tls
    result = _engine_connect(port, {"RAILWAY_ENVIRONMENT_NAME": "production"})
    assert "SSLCertVerificationError" in result.stdout, result.stdout + result.stderr
    assert not handshakes


def test_engine_verify_ca_completes_handshake_with_same_ca(fake_postgres_tls):
    # Control: proves the two refusals above come from certificate checks, not from the fake server.
    port, ca_path, handshakes = fake_postgres_tls
    result = _engine_connect(port, {"TALITRADE_DB_SSL_MODE": "verify-ca", "TALITRADE_DB_SSL_ROOT_CERT": ca_path})
    assert "SSLCertVerificationError" not in result.stdout, result.stdout + result.stderr
    assert handshakes


def test_engine_verify_full_completes_handshake_when_chain_and_host_name_match(fake_postgres_tls_matching_host):
    # Positive control for verify-full itself (production defaults: no mode set, Railway env present):
    # the same engine that refuses a wrong host name or an unknown CA accepts a valid, matching certificate.
    port, ca_path, handshakes = fake_postgres_tls_matching_host
    result = _engine_connect(port, {"TALITRADE_DB_SSL_ROOT_CERT": ca_path, "RAILWAY_ENVIRONMENT_NAME": "production"})
    assert "SSLCertVerificationError" not in result.stdout, result.stdout + result.stderr
    assert handshakes
