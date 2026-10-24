"""Phase 8 authentication and authorization primitives."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie
from pwdlib import PasswordHash

from app.config import Settings, get_settings
from app.contracts.enums import UserRole
from app.db.models import User

password_hash = PasswordHash.recommended()
cookie = APIKeyCookie(name="refund_session", auto_error=False)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user: User, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.id,
            "role": user.role,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
            "jti": str(uuid4()),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def current_user(
    request: Request,
    token: str | None = Depends(cookie),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication is required.")
    try:
        payload = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"])
        user_id = payload.get("sub")
        if not isinstance(user_id, str):
            raise ValueError
    except (jwt.InvalidTokenError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="The session is invalid or expired."
        ) from None
    session = request.app.state.session_factory()
    try:
        user = cast(User | None, session.get(User, user_id))
        if user is None:
            raise HTTPException(status_code=401, detail="The session is invalid or expired.")
        return user
    finally:
        session.close()


def require_role(role: UserRole) -> Callable[..., User]:
    def dependency(user: User = Depends(current_user)) -> User:  # noqa: B008
        if user.role != role.value:
            raise HTTPException(status_code=403, detail="You do not have permission for this operation.")
        return user

    return dependency
