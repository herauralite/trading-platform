import logging
import os
import ssl
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# Load .env explicitly from backend folder
load_dotenv(Path(__file__).parent.parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not (DATABASE_URL or "").strip():
    # Fail with a clear message instead of SQLAlchemy's opaque ArgumentError on None.
    raise RuntimeError("DATABASE_URL is not set: configure it in the service environment")

logger = logging.getLogger(__name__)

DB_SSL_MODES = ("require", "verify-ca", "verify-full")
DEFAULT_DB_SSL_MODE = "verify-full"
PRODUCTION_ENV_VALUES = {"production", "prod"}


def is_production_environment() -> bool:
    """True on any Railway deployment, or when TALITRADE_ENV says production.

    Railway sets RAILWAY_ENVIRONMENT_NAME (older projects: RAILWAY_ENVIRONMENT) on every deployment, so a
    hosted service is treated as production even if nobody remembered to set TALITRADE_ENV."""
    if (os.getenv("TALITRADE_ENV") or "").strip().lower() in PRODUCTION_ENV_VALUES:
        return True
    return bool((os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT") or "").strip())


def _default_ca_bundle() -> str | None:
    try:
        import certifi
        return certifi.where()
    except Exception:  # pragma: no cover - certifi is in requirements.txt
        return None


RAILWAY_PRIVATE_SUFFIX = ".railway.internal"


def database_host(url: str | None = None) -> str:
    """Lower-cased host of DATABASE_URL ('' if it cannot be parsed). Never logged in full by this module."""
    from sqlalchemy.engine import make_url
    try:
        return (make_url(url if url is not None else DATABASE_URL).host or "").strip().lower().rstrip(".")
    except Exception:
        return ""


def is_railway_private_host(host: str) -> bool:
    """True only for <name>.railway.internal: Railway's private network, which is WireGuard-encrypted and
    reachable only from services in the same project environment. A look-alike such as
    `db.railway.internal.example.com` does not match."""
    return host.endswith(RAILWAY_PRIVATE_SUFFIX) and len(host) > len(RAILWAY_PRIVATE_SUFFIX)


UNVERIFIED_EXCEPTION_ENV = "TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION"
UNVERIFIED_EXCEPTION_VALUE = "railway-private-network"
X509_STRICT_ENV = "TALITRADE_DB_SSL_X509_STRICT"


def effective_ssl_mode(mode: str | None = None, host: str | None = None) -> str:
    """The TLS mode to use: TALITRADE_DB_SSL_MODE if set, otherwise verify-full for every host.

    There is deliberately no automatic downgrade for *.railway.internal. Encrypted-but-unverified TLS there is
    an owner exception that has to be configured explicitly (see build_ssl_context). `host` is accepted for
    call-site compatibility and ignored."""
    raw = mode if mode is not None else os.getenv("TALITRADE_DB_SSL_MODE")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    return DEFAULT_DB_SSL_MODE


def _x509_strict(value: str | None) -> bool:
    raw = (value if value is not None else os.getenv(X509_STRICT_ENV) or "on").strip().lower()
    if raw in ("on", "1", "true", "yes"):
        return True
    if raw in ("off", "0", "false", "no"):
        return False
    raise ValueError(f"{X509_STRICT_ENV} must be 'on' or 'off'")


def build_ssl_context(mode: str | None = None, root_cert: str | None = None, root_cert_pem: str | None = None,
                      production: bool | None = None, host: str | None = None,
                      unverified_exception: str | None = None, x509_strict: str | None = None) -> ssl.SSLContext:
    """TLS settings for the database connection. Verification never silently falls back.

    TALITRADE_DB_SSL_MODE:
      verify-full  (default, every host) the server certificate chain must validate AND match the host name in
                   DATABASE_URL. Trust anchors: TALITRADE_DB_SSL_ROOT_CERT (PEM file path), or
                   TALITRADE_DB_SSL_ROOT_CERT_PEM (PEM text), or the certifi public CA bundle.
      verify-ca    chain must validate against the configured CA; host name is not compared.
      require      ENCRYPTED, SERVER IDENTITY NOT VERIFIED. In production this is refused unless the owner has
                   recorded the exception TALITRADE_DB_TLS_UNVERIFIED_EXCEPTION=railway-private-network AND the
                   host is <name>.railway.internal (WireGuard-encrypted private network).
    TALITRADE_DB_SSL_X509_STRICT (default on): Python 3.13+ enables OpenSSL's strict RFC 5280 profile checks,
      which reject a CA certificate that has no keyUsage extension. Railway's postgres-ssl image creates its
      root CA exactly that way (verify_code=92). `off` clears only that flag. Chain signatures, validity,
      basicConstraints and (verify-full) the host name are still checked: this is OpenSSL's and libpq's default
      policy. It is allowed only with a pinned CA, never with the public bundle.
    An unknown value, an unreadable/invalid CA, or a refused combination stops startup."""
    host = database_host() if host is None else host
    mode = effective_ssl_mode(mode, host)
    root_cert = root_cert if root_cert is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None
    root_cert_pem = root_cert_pem if root_cert_pem is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip() or None
    production = is_production_environment() if production is None else production
    exception = (unverified_exception if unverified_exception is not None
                 else os.getenv(UNVERIFIED_EXCEPTION_ENV) or "").strip().lower()
    strict = _x509_strict(x509_strict)
    if mode not in DB_SSL_MODES:
        raise ValueError(f"TALITRADE_DB_SSL_MODE must be one of {', '.join(DB_SSL_MODES)}")
    if root_cert and root_cert_pem:
        raise ValueError("Set only one of TALITRADE_DB_SSL_ROOT_CERT and TALITRADE_DB_SSL_ROOT_CERT_PEM")
    if exception and exception != UNVERIFIED_EXCEPTION_VALUE:
        raise ValueError(f"{UNVERIFIED_EXCEPTION_ENV} may only be '{UNVERIFIED_EXCEPTION_VALUE}'")
    if mode == "require":
        if production and not (exception == UNVERIFIED_EXCEPTION_VALUE and is_railway_private_host(host)):
            raise RuntimeError(
                "TALITRADE_DB_SSL_MODE=require (server identity NOT verified) is refused in production. It needs an "
                f"owner-approved exception: {UNVERIFIED_EXCEPTION_ENV}={UNVERIFIED_EXCEPTION_VALUE}, and the database "
                "host must be on Railway's private network (*.railway.internal)")
        if root_cert or root_cert_pem:
            raise ValueError("A database root certificate is set but TALITRADE_DB_SSL_MODE is 'require': choose verify-ca or verify-full")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if not strict and not (root_cert or root_cert_pem):
        raise ValueError(f"{X509_STRICT_ENV}=off requires a pinned CA (TALITRADE_DB_SSL_ROOT_CERT or _PEM)")
    if root_cert_pem:
        context = ssl.create_default_context(cadata=root_cert_pem)
    else:
        context = ssl.create_default_context(cafile=root_cert or _default_ca_bundle())
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = mode == "verify-full"
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not strict:
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


def describe_tls(context: ssl.SSLContext, host: str | None = None) -> str:
    """One sanitized line for the startup log: what is and is not verified. Never includes the host name."""
    host = database_host() if host is None else host
    pinned = bool((os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or (os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip())
    if context.verify_mode == ssl.CERT_NONE:
        return (f"Database TLS: encrypted, server identity NOT verified (mode=require, owner exception="
                f"{(os.getenv(UNVERIFIED_EXCEPTION_ENV) or '').strip() or 'none'}, "
                f"railway_private_network={is_railway_private_host(host)})")
    strict = bool(context.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))
    return (f"Database TLS: server identity verified (chain=yes, hostname={'yes' if context.check_hostname else 'no'}, "
            f"trust={'pinned CA' if pinned else 'public CA bundle'}, x509_strict={'on' if strict else 'off'})")


CA_EXPIRY_WARNING_DAYS = 60


def pinned_ca_days_left(root_cert: str | None = None, root_cert_pem: str | None = None) -> int | None:
    """Days until the earliest certificate in the configured CA bundle expires (None if no CA is configured).

    Railway's SSL Postgres regenerates its whole CA when the server certificate is within 30 days of expiry
    (820-day lifetime), so a pinned TALITRADE_DB_SSL_ROOT_CERT_PEM silently becomes wrong on that day and
    every new connection would fail verification. Startup logs this so the pin is renewed in time."""
    root_cert = root_cert if root_cert is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None
    root_cert_pem = root_cert_pem if root_cert_pem is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip() or None
    if not (root_cert or root_cert_pem):
        return None
    from datetime import datetime, timezone
    from cryptography import x509
    data = root_cert_pem.encode() if root_cert_pem else Path(root_cert).read_bytes()
    soonest = min(c.not_valid_after_utc for c in x509.load_pem_x509_certificates(data))
    return (soonest - datetime.now(timezone.utc)).days


ssl_context = build_ssl_context()
_ca_days = pinned_ca_days_left()
if _ca_days is not None:
    (logger.warning if _ca_days <= CA_EXPIRY_WARNING_DAYS else logger.info)(
        "Database TLS: pinned CA expires in %s days%s", _ca_days,
        "; Railway regenerates its Postgres CA ~30 days before server-certificate expiry: re-pin "
        "TALITRADE_DB_SSL_ROOT_CERT_PEM before then" if _ca_days <= CA_EXPIRY_WARNING_DAYS else "")
(logger.warning if ssl_context.verify_mode == ssl.CERT_NONE else logger.info)(describe_tls(ssl_context))

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    connect_args={"ssl": ssl_context},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
