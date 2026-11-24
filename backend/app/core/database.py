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


def effective_ssl_mode(mode: str | None = None, host: str | None = None) -> str:
    """The TLS mode to use. Explicit TALITRADE_DB_SSL_MODE wins. Unset, it is verify-full, except on Railway's
    private network, where Railway's Postgres presents a certificate from its own generated CA that public
    trust stores cannot verify (see 2026-11-24: verify-full by default crash-looped production)."""
    raw = mode if mode is not None else os.getenv("TALITRADE_DB_SSL_MODE")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    host = database_host() if host is None else host
    return "require" if is_railway_private_host(host) else DEFAULT_DB_SSL_MODE


def build_ssl_context(mode: str | None = None, root_cert: str | None = None, root_cert_pem: str | None = None,
                      production: bool | None = None, host: str | None = None) -> ssl.SSLContext:
    """TLS settings for the database connection. Verification never silently falls back.

    TALITRADE_DB_SSL_MODE:
      verify-full  (default for every host except *.railway.internal) the server certificate chain must
                   validate AND match the host name in DATABASE_URL. Trust anchors:
                   TALITRADE_DB_SSL_ROOT_CERT (PEM file path), or TALITRADE_DB_SSL_ROOT_CERT_PEM (PEM text),
                   or the certifi public CA bundle (correct for providers with publicly trusted certificates).
      verify-ca    chain must validate against the configured CA; host name is not compared. Use with a
                   private CA whose server certificate does not name the host.
      require      encrypted, server NOT authenticated. In production this is refused, except for a
                   *.railway.internal host (private, WireGuard-encrypted network), where it is the default.
    An unknown mode, an unreadable/invalid CA, or `require` to a non-private host in production stops startup."""
    host = database_host() if host is None else host
    mode = effective_ssl_mode(mode, host)
    root_cert = root_cert if root_cert is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None
    root_cert_pem = root_cert_pem if root_cert_pem is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip() or None
    production = is_production_environment() if production is None else production
    if mode not in DB_SSL_MODES:
        raise ValueError(f"TALITRADE_DB_SSL_MODE must be one of {', '.join(DB_SSL_MODES)}")
    if root_cert and root_cert_pem:
        raise ValueError("Set only one of TALITRADE_DB_SSL_ROOT_CERT and TALITRADE_DB_SSL_ROOT_CERT_PEM")
    if mode == "require":
        if production and not is_railway_private_host(host):
            raise RuntimeError("TALITRADE_DB_SSL_MODE=require is not allowed in production for a database outside "
                               "Railway's private network: use verify-full (default) so the server is authenticated")
        if root_cert or root_cert_pem:
            raise ValueError("A database root certificate is set but TALITRADE_DB_SSL_MODE is 'require': choose verify-ca or verify-full")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if root_cert_pem:
        context = ssl.create_default_context(cadata=root_cert_pem)
    else:
        context = ssl.create_default_context(cafile=root_cert or _default_ca_bundle())
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = mode == "verify-full"
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


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
if ssl_context.verify_mode == ssl.CERT_NONE:
    logger.warning("Database TLS: encrypted, server certificate NOT verified (mode=require, railway_private_network=%s)",
                   is_railway_private_host(database_host()))
else:
    logger.info("Database TLS: certificate verification on (hostname check=%s)", ssl_context.check_hostname)

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
