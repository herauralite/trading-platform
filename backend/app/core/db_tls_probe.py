"""Read-only TLS probe of the configured PostgreSQL server. Run where the app runs (e.g. a Railway shell):

    python -m app.core.db_tls_probe                    # report only
    python -m app.core.db_tls_probe --print-presented-ca   # also print a self-signed CA the server presents

It opens one TCP connection, sends the PostgreSQL SSLRequest, completes a TLS handshake and closes. It never
authenticates, never sends the user name or password, and never prints DATABASE_URL: only the host's last two
labels, the port, the certificates' public facts (subject, issuer, names, validity, fingerprint), and PASS/FAIL
for each verification mode with OpenSSL's reason. Certificates are public data; the output is safe to share.

Checks:
  public-ca/verify-full      certifi bundle, chain + host name (the strict default for public hosts)
  configured-ca/verify-ca    TALITRADE_DB_SSL_ROOT_CERT(_PEM), chain only          (if a CA is configured)
  configured-ca/verify-full  TALITRADE_DB_SSL_ROOT_CERT(_PEM), chain + host name   (if a CA is configured)
  presented-ca/verify-full   the self-signed root the server itself sent, chain + host name. This one proves
                             nothing about trust (the CA came over the same connection); it only shows whether
                             the certificate NAMES the host, i.e. whether verify-full can ever pass with that CA.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import ssl
import struct
import sys
from typing import Any

SSL_REQUEST = struct.pack("!ii", 8, 80877103)
TIMEOUT = 10.0


def _target(url: str) -> tuple[str, int]:
    from sqlalchemy.engine import make_url
    u = make_url(url)
    return (u.host or "").strip(), int(u.port or 5432)


def _suffix(host: str) -> str:
    parts = host.rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else "(single-label host)"


def _reason(exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"verify_code={exc.verify_code} " + ((exc.verify_message or "").split(",", 1)[0].strip() or "unknown")
    return type(exc).__name__


def _tls_socket(host: str, port: int, context: ssl.SSLContext, server_hostname: str | None) -> ssl.SSLSocket:
    raw = socket.create_connection((host, port), timeout=TIMEOUT)
    try:
        raw.sendall(SSL_REQUEST)
        answer = raw.recv(1)
        if answer != b"S":
            raise ConnectionError("server_refused_ssl" if answer == b"N" else "unexpected_sslrequest_answer")
        return context.wrap_socket(raw, server_hostname=server_hostname)
    except BaseException:
        raw.close()
        raise


def _describe(der: bytes) -> dict[str, Any]:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    cert = x509.load_der_x509_certificate(der)

    def cn(name):
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else name.rfc4514_string()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return {"subject_cn": cn(cert.subject), "issuer_cn": cn(cert.issuer), "san": names,
            "self_signed": cert.subject == cert.issuer, "not_after": cert.not_valid_after_utc.isoformat(),
            "sha256": hashlib.sha256(der).hexdigest()[:32], "_der": der}


def presented_chain(host: str, port: int) -> list[dict[str, Any]]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with _tls_socket(host, port, ctx, host) as tls:
        chain = []
        getter = getattr(tls, "get_unverified_chain", None)     # Python 3.13+
        if getter is not None:
            for item in getter() or []:
                if isinstance(item, (bytes, bytearray)):
                    chain.append(bytes(item))
        if not chain:
            chain = [tls.getpeercert(binary_form=True)]
    return [_describe(der) for der in chain]


def check(host: str, port: int, *, cafile: str | None = None, cadata: str | None = None, hostname: bool) -> str:
    ctx = ssl.create_default_context(cafile=cafile, cadata=cadata)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = hostname
    try:
        with _tls_socket(host, port, ctx, host):
            return "PASS"
    except Exception as exc:
        return "FAIL " + _reason(exc)


def probe(url: str, *, root_cert: str | None = None, root_cert_pem: str | None = None) -> dict[str, Any]:
    import certifi
    host, port = _target(url)
    report: dict[str, Any] = {"host_suffix": _suffix(host), "port": port}
    chain = presented_chain(host, port)
    report["presented"] = [{k: v for k, v in c.items() if k != "_der"} for c in chain]
    checks = {"public-ca/verify-full": check(host, port, cafile=certifi.where(), hostname=True)}
    if root_cert or root_cert_pem:
        checks["configured-ca/verify-ca"] = check(host, port, cafile=root_cert, cadata=root_cert_pem, hostname=False)
        checks["configured-ca/verify-full"] = check(host, port, cafile=root_cert, cadata=root_cert_pem, hostname=True)
    roots = [c for c in chain if c["self_signed"]]
    if roots:
        pem = ssl.DER_cert_to_PEM_cert(roots[-1]["_der"])
        checks["presented-ca/verify-full (names only, not trust)"] = check(host, port, cadata=pem, hostname=True)
        report["_presented_ca_pem"] = pem
    report["checks"] = checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-presented-ca", action="store_true",
                        help="print the PEM of a self-signed CA the server presents (public data)")
    args = parser.parse_args(argv)
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set")
        return 2
    try:
        report = probe(url, root_cert=(os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None,
                       root_cert_pem=(os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip() or None)
    except Exception as exc:
        print(f"probe failed before TLS completed: {_reason(exc)}")
        return 1
    print(f"host_suffix={report['host_suffix']} port={report['port']}")
    for i, c in enumerate(report["presented"]):
        print(f"cert[{i}] subject_cn={c['subject_cn']!r} issuer_cn={c['issuer_cn']!r} self_signed={c['self_signed']} "
              f"san={c['san']} not_after={c['not_after']} sha256[:32]={c['sha256']}")
    for name, result in report["checks"].items():
        print(f"check {name}: {result}")
    if args.print_presented_ca and report.get("_presented_ca_pem"):
        print(report["_presented_ca_pem"], end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
