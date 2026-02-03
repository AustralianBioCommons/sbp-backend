"""Shared FastAPI dependencies for route modules."""

from __future__ import annotations

from collections.abc import Generator
from typing import cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..db.models.core import AppUser


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user_id(
    x_auth0_user_id: str | None = Header(None, alias="X-Auth0-User-Id"),
    db: Session = Depends(get_db),
) -> UUID:
    if not x_auth0_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Auth0-User-Id header",
        )

    user = db.execute(
        select(AppUser).where(AppUser.auth0_user_id == x_auth0_user_id)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown user",
        )

    return cast(UUID, user.id)
