"""Shared FastAPI dependencies for route modules."""

from __future__ import annotations

from collections.abc import Generator
from typing import cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.validator import verify_access_token_sub
from ..db import SessionLocal
from ..db.models.core import AppUser


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user_id(
    authorization: str | None = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> UUID:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format; expected Bearer token",
        )

    auth0_user_id = verify_access_token_sub(token.strip())
    user = db.execute(
        select(AppUser).where(AppUser.auth0_user_id == auth0_user_id)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
        )

    return cast(UUID, user.id)
