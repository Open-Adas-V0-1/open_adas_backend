"""Auth dependency (T6b Step 1) -- guards every endpoint that requires a logged-in
user. Implemented as a FastAPI dependency (Depends), not ASGI middleware: it needs to
inject the resolved User object into route handler signatures, which ASGI middleware
cannot do cleanly.

Every failure mode (missing header, malformed token, expired token, unknown user)
collapses to the SAME generic 401 -- no detail is given that would let a client
distinguish why, keeping this dependency simple and consistent for every future
endpoint that reuses it.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.security import decode_access_token
from data.db import get_session
from data.models import User
from data.repository import UserRepo

_bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise _UNAUTHORIZED

    try:
        user_id = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise _UNAUTHORIZED from exc

    user = await UserRepo.get_by_id(db, user_id)
    if user is None:
        raise _UNAUTHORIZED

    return user
