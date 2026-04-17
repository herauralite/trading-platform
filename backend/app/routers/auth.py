import hashlib
import hmac
import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.core.database import get_db
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


class TelegramAuthRequest(BaseModel):
    telegram_id: int
    first_name: str | None = None
    username: str | None = None
    auth_date: int
    hash: str
    query_string: str


def verify_telegram_hash(data: TelegramAuthRequest) -> bool:
    if not BOT_TOKEN:
        return False
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    is_valid = hmac.compare_digest(
        hmac.new(secret_key, data.query_string.encode(), hashlib.sha256).hexdigest(),
        data.hash
    )
    return is_valid


def create_token(user_id: str) -> str:
    if not SECRET_KEY:
        raise HTTPException(status_code=500, detail="Server auth is not configured")
    expire = datetime.utcnow() + timedelta(minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")))
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/telegram")
async def telegram_auth(data: TelegramAuthRequest, db: AsyncSession = Depends(get_db)):
    if not verify_telegram_hash(data):
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")

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
