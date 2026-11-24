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


def build_ssl_context(mode: str | None = None, root_cert: str | None = None, root_cert_pem: str | None = None,
                      production: bool | None = None) -> ssl.SSLContext:
    """TLS settings for the database connection. Verification is the default and never silently falls back.

    TALITRADE_DB_SSL_MODE:
      verify-full  (default) the server certificate chain must validate AND match the host name in
                   DATABASE_URL. Trust anchors: TALITRADE_DB_SSL_ROOT_CERT (PEM file path), or
                   TALITRADE_DB_SSL_ROOT_CERT_PEM (PEM text, for platforms without file mounts), or the
                   certifi public CA bundle (correct for Neon, whose certificates are publicly trusted).
      verify-ca    chain must validate; host name is not compared. Use only with a private CA.
      require      encrypted but the server is NOT authenticated. Local development only: refused when
                   is_production_environment() is true, so a deploy can never run unverified.
    An unknown mode, an unreadable/invalid CA, or `require` in production stops startup."""
    mode = (mode if mode is not None else os.getenv("TALITRADE_DB_SSL_MODE") or DEFAULT_DB_SSL_MODE).strip().lower()
    root_cert = root_cert if root_cert is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None
    root_cert_pem = root_cert_pem if root_cert_pem is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT_PEM") or "").strip() or None
    production = is_production_environment() if production is None else production
    if mode not in DB_SSL_MODES:
        raise ValueError(f"TALITRADE_DB_SSL_MODE must be one of {', '.join(DB_SSL_MODES)}")
    if root_cert and root_cert_pem:
        raise ValueError("Set only one of TALITRADE_DB_SSL_ROOT_CERT and TALITRADE_DB_SSL_ROOT_CERT_PEM")
    if mode == "require":
        if production:
            raise RuntimeError("TALITRADE_DB_SSL_MODE=require is not allowed in production: "
                               "use verify-full (default) so the database server is authenticated")
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


ssl_context = build_ssl_context()
if ssl_context.verify_mode == ssl.CERT_NONE:
    logger.warning("Database TLS: encrypted, but the server certificate is NOT verified (TALITRADE_DB_SSL_MODE=require). "
                   "This is only permitted outside production.")
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
