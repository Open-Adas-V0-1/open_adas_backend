<<<<<<< Updated upstream
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: uuid.UUID | str) -> str:
    """Minimal JWT access token (T6b Step 1) -- no refresh tokens. The subject is
    ALWAYS the user's id, never the email -- callers (get_current_user) must never
    have to trust/parse an email out of the token.
    """
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID:
    """Decodes and validates a JWT access token (signature + expiry), returning the
    user id it was issued for. Raises ValueError on ANY failure (bad signature,
    expired, malformed, missing/invalid subject) -- callers (get_current_user)
    collapse every failure mode to the same generic 401, never distinguishing why.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("invalid or expired token") from exc

    subject = payload.get("sub")
    if not subject:
        raise ValueError("token has no subject")
    try:
        return uuid.UUID(subject)
    except ValueError as exc:
        raise ValueError("token subject is not a valid user id") from exc
=======
import bcrypt


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
>>>>>>> Stashed changes
