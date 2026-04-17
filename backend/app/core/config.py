import json
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

DEFAULT_FRONTEND_ALLOWED_ORIGINS = (
    "https://www.talitrade.com",
    "https://talitrade.com",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
)


def normalize_origin(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not scheme or not host:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{host}{port}"


def parse_frontend_allowed_origins(raw: str | None) -> list[str]:
    source = str(raw or "").strip()
    if not source:
        return list(DEFAULT_FRONTEND_ALLOWED_ORIGINS)

    candidates: list[str] = []
    if source.startswith("["):
        try:
            parsed = json.loads(source)
            if isinstance(parsed, list):
                candidates = [str(item) for item in parsed]
        except json.JSONDecodeError:
            candidates = []
    if not candidates:
        candidates = re.split(r"[\n,]", source)

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        origin = normalize_origin(candidate)
        if not origin or origin in seen:
            continue
        seen.add(origin)
        normalized.append(origin)
    return normalized or list(DEFAULT_FRONTEND_ALLOWED_ORIGINS)


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    FRONTEND_ALLOWED_ORIGINS_RAW: str = os.getenv("FRONTEND_ALLOWED_ORIGINS", "")
    FRONTEND_ALLOWED_ORIGINS: list[str] = parse_frontend_allowed_origins(FRONTEND_ALLOWED_ORIGINS_RAW)


settings = Settings()
