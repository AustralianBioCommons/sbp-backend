"""User account and credit balance routes."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db.models.core import AppUser
from .dependencies import get_current_user_id, get_db

router = APIRouter(tags=["users"])


class UserCreditResponse(BaseModel):
    """Credit balance for a single user."""

    userId: str = Field(..., description="Application user ID")
    credit: int = Field(..., description="Remaining user credit balance")


class SelfCreditUpdateRequest(BaseModel):
    """Request for a user to set their own remaining credit balance."""

    credit: int = Field(..., ge=0, description="New remaining user credit balance")


@router.get("/me/credit", response_model=UserCreditResponse)
def get_my_credit(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> UserCreditResponse:
    """Return the authenticated user's remaining credit balance."""
    credit = db.scalar(select(AppUser.credit).where(AppUser.id == current_user_id))
    if credit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserCreditResponse(userId=str(current_user_id), credit=credit)


@router.put("/me/credit", response_model=UserCreditResponse)
def update_my_credit(
    payload: SelfCreditUpdateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> UserCreditResponse:
    """Set the authenticated user's own remaining credit balance.

    Self-service (no admin required), used when a workflow run is confirmed and
    its cost is deducted. The new balance may only stay the same or decrease —
    a user can never grant themselves credits. ``credit_updated_by`` records the
    acting user.
    """
    row = db.execute(
        select(AppUser.email, AppUser.credit).where(AppUser.id == current_user_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.credit > row.credit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credits can only be decreased",
        )

    db.execute(
        update(AppUser)
        .where(AppUser.id == current_user_id)
        .values(
            credit=payload.credit,
            credit_updated_at=datetime.now(timezone.utc),
            credit_updated_by=row.email,
        )
    )
    db.commit()

    return UserCreditResponse(userId=str(current_user_id), credit=payload.credit)
