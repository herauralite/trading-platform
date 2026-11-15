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

logger = logging.getLogger(__name__)

DB_SSL_MODES = ("require", "verify-ca", "verify-full")


def build_ssl_context(mode: str | None = None, root_cert: str | None = None) -> ssl.SSLContext:
    """TLS settings for the database connection.

    TALITRADE_DB_SSL_MODE:
      require      (default, the behaviour this app has always had) the connection is encrypted but
                   the server's certificate is NOT checked, so the server is not authenticated: an
                   attacker able to intercept the connection could impersonate the database.
      verify-ca    the certificate chain must validate against TALITRADE_DB_SSL_ROOT_CERT (a PEM file)
                   or, without it, the system trust store. The host name is not compared.
      verify-full  verify-ca plus the certificate must match the host name in DATABASE_URL.
    An unknown mode, or a root certificate file that cannot be loaded, stops startup: it never falls
    back to an unverified connection."""
    mode = (mode if mode is not None else os.getenv("TALITRADE_DB_SSL_MODE") or "require").strip().lower()
    root_cert = root_cert if root_cert is not None else (os.getenv("TALITRADE_DB_SSL_ROOT_CERT") or "").strip() or None
    if mode not in DB_SSL_MODES:
        raise ValueError(f"TALITRADE_DB_SSL_MODE must be one of {', '.join(DB_SSL_MODES)}")
    if mode == "require":
        if root_cert:
            raise ValueError("TALITRADE_DB_SSL_ROOT_CERT is set but TALITRADE_DB_SSL_MODE is 'require': choose verify-ca or verify-full")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    context = ssl.create_default_context(cafile=root_cert) if root_cert else ssl.create_default_context()
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = mode == "verify-full"
    return context


ssl_context = build_ssl_context()
if ssl_context.verify_mode == ssl.CERT_NONE:
    logger.warning("Database TLS: encrypted, but the server certificate is NOT verified (TALITRADE_DB_SSL_MODE=require). "
                   "Set verify-full with TALITRADE_DB_SSL_ROOT_CERT to authenticate the database server.")

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
