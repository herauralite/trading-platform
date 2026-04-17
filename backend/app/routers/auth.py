import hashlib
import hmac
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])

ALGORITHM = os.getenv("ALGORITHM", "HS256")


class TelegramAuthRequest(BaseModel):
    telegram_id: int
    first_name: str | None = None
    username: str | None = None
    auth_date: int
    hash: str
    query_string: str


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required auth configuration: {name}")
    return value


def verify_telegram_hash(data: TelegramAuthRequest) -> bool:
    bot_token = _get_required_env("TELEGRAM_BOT_TOKEN")
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    is_valid = hmac.compare_digest(
        hmac.new(secret_key, data.query_string.encode(), hashlib.sha256).hexdigest(),
        data.hash,
    )
    return is_valid


def create_token(user_id: str) -> str:
    secret_key = _get_required_env("SECRET_KEY")
    expire = datetime.utcnow() + timedelta(
        minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    )
    return jwt.encode({"sub": user_id, "exp": expire}, secret_key, algorithm=ALGORITHM)


@router.post("/telegram")
async def telegram_auth(data: TelegramAuthRequest, db: AsyncSession = Depends(get_db)):
    try:
        if not verify_telegram_hash(data):
            raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = await db.execute(select(User).where(User.telegram_id == data.telegram_id))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            telegram_id=data.telegram_id,
            first_name=data.first_name,
            username=data.username,
        )
        db.add(user)
        await db.flush()

    token = create_token(str(user.id))
    return {"access_token": token, "token_type": "bearer", "user_id": str(user.id)}
