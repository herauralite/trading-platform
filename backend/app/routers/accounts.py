import uuid
import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from jose import jwt, JWTError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.database import get_db
from app.models.linked_account import LinkedAccount

router = APIRouter(prefix="/accounts", tags=["accounts"])
security = HTTPBearer()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return uuid.UUID(user_id)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


class AddAccountRequest(BaseModel):
    account_login: str
    password: str
    server: str
    display_name: str | None = None


class SetPrimaryRequest(BaseModel):
    account_id: str


@router.get("/")
async def list_accounts(
    user_id: uuid.UUID = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(LinkedAccount).where(
            LinkedAccount.user_id == user_id,
            LinkedAccount.is_active == True
        )
    )
    accounts = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "account_login": a.account_login,
            "server": a.server,
            "display_name": a.display_name,
            "is_primary": a.is_primary,
            "is_funded": a.is_funded,
            "broker_type": a.broker_type,
        }
        for a in accounts
    ]


@router.post("/")
async def add_account(
    data: AddAccountRequest,
    user_id: uuid.UUID = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    account = LinkedAccount(
        user_id=user_id,
        account_login=data.account_login,
        encrypted_password=data.password,  # Encryption added later
        server=data.server,
        display_name=data.display_name,
        broker_type="matchtrade",
    )
    db.add(account)
    await db.flush()
    return {"id": str(account.id), "message": "Account added successfully"}


@router.post("/set-primary")
async def set_primary(
    data: SetPrimaryRequest,
    user_id: uuid.UUID = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(LinkedAccount).where(LinkedAccount.user_id == user_id)
    )
    accounts = result.scalars().all()
    for a in accounts:
        a.is_primary = str(a.id) == data.account_id
    return {"message": "Primary account updated"}
