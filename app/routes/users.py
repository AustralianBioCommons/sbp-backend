"""User account and credit balance routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.admin import require_admin_access
from ..db.models.core import AppUser
from .dependencies import get_current_user_id, get_db

router = APIRouter(tags=["users"])


class UserCreditResponse(BaseModel):
    """Credit balance for a single user."""

    userId: str = Field(..., description="Application user ID")
    credit: int = Field(..., description="Remaining user credit balance")


class UserCreditListItem(BaseModel):
    """Admin-facing user credit balance entry."""

    auth0UserId: str
    name: str
    email: str
    credit: int = Field(..., description="Remaining user credit balance")


class UserCreditListResponse(BaseModel):
    """Paginated user credit balance listing."""

    users: list[UserCreditListItem]
    total: int
    limit: int
    offset: int


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


@router.get(
    "/credits",
    response_model=UserCreditListResponse,
    dependencies=[Depends(require_admin_access)],
)
def list_user_credits(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> UserCreditListResponse:
    """Return a paginated list of user credit balances for administrators."""
    total = db.execute(select(func.count()).select_from(AppUser)).scalar_one()
    users = db.execute(
        select(AppUser.auth0_user_id, AppUser.name, AppUser.email, AppUser.credit)
        .order_by(AppUser.email)
        .offset(offset)
        .limit(limit)
    )

    return UserCreditListResponse(
        users=[
            UserCreditListItem(
                auth0UserId=user.auth0_user_id,
                name=user.name,
                email=user.email,
                credit=user.credit,
            )
            for user in users
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
