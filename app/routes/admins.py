"""Admin user account and credit balance routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db.admin import require_admin_access
from ..db.models.core import AppUser
from .dependencies import get_db

router = APIRouter(
    tags=["users"],
    dependencies=[Depends(require_admin_access)],
)


class UserCreditListItem(BaseModel):
    """Admin-facing user credit balance entry."""

    auth0UserId: str
    name: str
    email: str
    credit: int = Field(..., description="Remaining user credit balance")
    creditUpdatedAt: datetime | None = Field(
        default=None, description="Timestamp of the most recent credit update"
    )
    creditUpdatedBy: str | None = Field(
        default=None, description="Admin actor who most recently updated credit"
    )


class UserCreditListResponse(BaseModel):
    """Paginated user credit balance listing."""

    users: list[UserCreditListItem]
    total: int
    page: int
    perPage: int


class UserCreditUpdateRequest(BaseModel):
    """Admin request to set a user's credit balance."""

    credit: int = Field(..., ge=0, description="New remaining user credit balance")


def _user_credit_item(
    auth0_user_id: str,
    name: str,
    email: str,
    credit: int,
    credit_updated_at: datetime | None,
    credit_updated_by: str | None,
) -> UserCreditListItem:
    return UserCreditListItem(
        auth0UserId=auth0_user_id,
        name=name,
        email=email,
        credit=credit,
        creditUpdatedAt=credit_updated_at,
        creditUpdatedBy=credit_updated_by,
    )


def _admin_actor(claims: dict[str, object]) -> str | None:
    for key in ("email", "sub", "name", "nickname"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@router.get(
    "/credits",
    response_model=UserCreditListResponse,
)
def list_user_credits(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> UserCreditListResponse:
    """Return a paginated list of user credit balances for administrators."""
    offset = (page - 1) * per_page
    total = db.execute(select(func.count()).select_from(AppUser)).scalar_one()
    users = db.execute(
        select(
            AppUser.auth0_user_id,
            AppUser.name,
            AppUser.email,
            AppUser.credit,
            AppUser.credit_updated_at,
            AppUser.credit_updated_by,
        )
        .order_by(AppUser.email)
        .offset(offset)
        .limit(per_page)
    )

    return UserCreditListResponse(
        users=[
            UserCreditListItem(
                auth0UserId=user.auth0_user_id,
                name=user.name,
                email=user.email,
                credit=user.credit,
                creditUpdatedAt=user.credit_updated_at,
                creditUpdatedBy=user.credit_updated_by,
            )
            for user in users
        ],
        total=total,
        page=page,
        perPage=per_page,
    )


@router.get(
    "/credits/{auth0_user_id:path}",
    response_model=UserCreditListItem,
)
def get_user_credit(
    auth0_user_id: str,
    db: Session = Depends(get_db),
) -> UserCreditListItem:
    """Return one user's credit balance for administrators."""
    user = db.execute(
        select(
            AppUser.auth0_user_id,
            AppUser.name,
            AppUser.email,
            AppUser.credit,
            AppUser.credit_updated_at,
            AppUser.credit_updated_by,
        ).where(AppUser.auth0_user_id == auth0_user_id)
    ).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return _user_credit_item(
        user.auth0_user_id,
        user.name,
        user.email,
        user.credit,
        user.credit_updated_at,
        user.credit_updated_by,
    )


@router.put(
    "/credits/{auth0_user_id:path}",
    response_model=UserCreditListItem,
)
def update_user_credit(
    auth0_user_id: str,
    payload: UserCreditUpdateRequest,
    admin_claims: dict[str, object] = Depends(require_admin_access),
    db: Session = Depends(get_db),
) -> UserCreditListItem:
    """Set one user's credit balance for administrators."""
    user = db.execute(
        select(AppUser.auth0_user_id, AppUser.name, AppUser.email).where(
            AppUser.auth0_user_id == auth0_user_id
        )
    ).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    updated_at = datetime.now(timezone.utc)
    updated_by = _admin_actor(admin_claims)
    db.execute(
        update(AppUser)
        .where(AppUser.auth0_user_id == auth0_user_id)
        .values(
            credit=payload.credit,
            credit_updated_at=updated_at,
            credit_updated_by=updated_by,
        )
    )
    db.commit()

    return _user_credit_item(
        user.auth0_user_id,
        user.name,
        user.email,
        payload.credit,
        updated_at,
        updated_by,
    )
