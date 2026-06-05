"""User account and credit balance routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.core import AppUser
from .dependencies import get_current_user_id, get_db

router = APIRouter(tags=["users"])


class UserCreditResponse(BaseModel):
    """Credit balance for a single user."""

    userId: str = Field(..., description="Application user ID")
    credit: int = Field(..., description="Remaining user credit balance")


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
