from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.middleware.auth import get_current_user
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.security import create_access_token, hash_password, verify_password
from data.db import get_session
from data.models import User
from data.repository import UserRepo

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# No user enumeration (T6b Step 1): wrong password and unknown email are
# INDISTINGUISHABLE to the caller -- same status, same message, same code path.
_INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_session)) -> User:
    existing = await UserRepo.get_by_email(db, payload.email)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = await UserRepo.create(db, email=payload.email, password_hash=hash_password(payload.password))
    await db.commit()
    logger.info("auth.register", user_id=str(user.id))
    return user


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await UserRepo.get_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.info("auth.login_failed", email=payload.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS_MESSAGE)

    logger.info("auth.login", user_id=str(user.id))
    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
