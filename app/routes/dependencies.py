"""Shared FastAPI dependencies for route modules."""

from __future__ import annotations

from collections.abc import Generator
from typing import cast
from uuid import UUID, uuid4

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth.validator import fetch_userinfo_claims, verify_access_token_claims
from ..db import SessionLocal
from ..db.models.core import AppUser

security = HTTPBearer()


def _extract_name_email_from_claims(
    auth0_user_id: str,
    claims: dict[str, object],
    token: str,
) -> tuple[str, str]:
    name_claim = claims.get("name") or claims.get("nickname")
    email_claim = claims.get("email")

    if not (isinstance(name_claim, str) and name_claim.strip()) or not (
        isinstance(email_claim, str) and email_claim.strip()
    ):
        userinfo = fetch_userinfo_claims(token)
        if not (isinstance(name_claim, str) and name_claim.strip()):
            name_claim = userinfo.get("name") or userinfo.get("nickname")
        if not (isinstance(email_claim, str) and email_claim.strip()):
            email_claim = userinfo.get("email")

    name = str(name_claim).strip() if isinstance(name_claim, str) and name_claim.strip() else auth0_user_id
    if isinstance(email_claim, str) and email_claim.strip():
        email = email_claim.strip().lower()
    else:
        safe_sub = auth0_user_id.replace("|", "_")
        email = f"{safe_sub}@unknown.local"

    return name, email


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> UUID:
    # HTTPBearer automatically extracts the token from "Bearer <token>"
    token = credentials.credentials

    claims = verify_access_token_claims(token)
    auth0_user_id = cast(str, claims["sub"])
    user = db.execute(
        select(AppUser).where(AppUser.auth0_user_id == auth0_user_id)
    ).scalar_one_or_none()

    name, email = _extract_name_email_from_claims(auth0_user_id, claims, token)

    if not user:
        user = AppUser(
            id=uuid4(),
            auth0_user_id=auth0_user_id,
            name=name,
            email=email,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Handle race where another request inserts the same auth0_user_id concurrently.
            db.rollback()
            existing = db.execute(
                select(AppUser).where(AppUser.auth0_user_id == auth0_user_id)
            ).scalar_one_or_none()
            if existing is None:
                raise
            user = existing
    else:
        # Refresh profile fields when we have better info than the placeholder values.
        should_update = False
        if user.name == auth0_user_id and name != auth0_user_id:
            user.name = name
            should_update = True
        if user.email.endswith("@unknown.local") and not email.endswith("@unknown.local"):
            user.email = email
            should_update = True
        if should_update:
            db.commit()

    return cast(UUID, user.id)
