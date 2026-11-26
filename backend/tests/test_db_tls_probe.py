"""Read-only TLS probe (app.core.db_tls_probe): real handshakes against a local fake PostgreSQL TLS server with a
throwaway CA. Proves each verification mode is reported and that the DSN's user/password never reach the output."""
import contextlib
import datetime
import ipaddress
import os
import socket
import ssl
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import db_tls_probe  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TALITRADE_DB_SSL_ROOT_CERT", "TALITRADE_DB_SSL_ROOT_CERT_PEM"):
        monkeypatch.delenv(key, raising=False)


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


def test_probe_reports_each_mode_and_never_prints_credentials(fake_postgres_tls_matching_host, capsys, monkeypatch):
    port, ca_path, _ = fake_postgres_tls_matching_host
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://probeuser:CANARY-probe-pw7@127.0.0.1:{port}/db")
    monkeypatch.setenv("TALITRADE_DB_SSL_ROOT_CERT_PEM", open(ca_path).read())
    monkeypatch.delenv("TALITRADE_DB_SSL_ROOT_CERT", raising=False)
    assert db_tls_probe.main([]) == 0
    out = capsys.readouterr().out
    assert "CANARY-probe-pw7" not in out and "probeuser" not in out and "postgresql" not in out
    assert "check public-ca/verify-full: FAIL" in out
    assert "check configured-ca/verify-ca: PASS" in out and "check configured-ca/verify-full: PASS" in out


def test_probe_shows_when_a_certificate_does_not_name_the_host(fake_postgres_tls, capsys, monkeypatch):
    # Certificate for "db.test" only, reached as 127.0.0.1: chain-only passes, hostname check fails.
    port, ca_path, _ = fake_postgres_tls
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:CANARY-probe-pw8@127.0.0.1:{port}/db")
    monkeypatch.setenv("TALITRADE_DB_SSL_ROOT_CERT", ca_path)
    monkeypatch.delenv("TALITRADE_DB_SSL_ROOT_CERT_PEM", raising=False)
    assert db_tls_probe.main([]) == 0
    out = capsys.readouterr().out
    assert "check configured-ca/verify-ca: PASS" in out
    assert "check configured-ca/verify-full: FAIL verify_code=" in out and "mismatch" in out.lower()
    assert "subject_cn='db.test'" in out


    assert "CANARY-probe-pw8" not in out


def test_probe_without_database_url_reports_and_exits(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "")
    assert db_tls_probe.main([]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().out


def test_probe_also_reports_results_with_the_strict_x509_flag_cleared(fake_postgres_tls_matching_host, capsys, monkeypatch):
    # The non-strict rows show what TALITRADE_DB_SSL_X509_STRICT=off (#154) would do. Chain and host name are
    # still verified there, so a CA that does not sign the server certificate must still fail.
    port, ca_path, _ = fake_postgres_tls_matching_host
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:CANARY-probe-pw11@127.0.0.1:{port}/db")
    monkeypatch.setenv("TALITRADE_DB_SSL_ROOT_CERT", ca_path)
    assert db_tls_probe.main([]) == 0
    out = capsys.readouterr().out
    assert "CANARY-probe-pw11" not in out
    assert "check configured-ca/verify-ca x509_strict=off: PASS" in out
    assert "check configured-ca/verify-full x509_strict=off: PASS" in out


def test_non_strict_check_still_rejects_an_untrusted_ca(fake_postgres_tls_matching_host, tmp_path):
    port, _, _ = fake_postgres_tls_matching_host
    other = tmp_path / "other"
    other.mkdir()
    _, wrong_ca = _make_ca_and_server_cert(other)
    result = db_tls_probe.check("127.0.0.1", port, cadata=wrong_ca, hostname=True, x509_strict=False)
    assert result.startswith("FAIL verify_code=")
