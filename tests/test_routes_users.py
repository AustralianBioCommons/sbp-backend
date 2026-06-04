"""Tests for user credit routes."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.admin import require_admin_access
from app.db.models.core import AppUser

TEST_USER_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_get_my_credit(client: TestClient, test_engine):
    """Current user can query their own credit balance."""
    with Session(test_engine) as db:
        db.execute(
            update(AppUser).where(AppUser.auth0_user_id == "auth0|test-user").values(credit=25)
        )
        db.commit()

    response = client.get("/api/users/me/credit")

    assert response.status_code == 200
    assert response.json() == {
        "userId": str(TEST_USER_ID),
        "credit": 25,
    }


def test_list_user_credits_requires_admin(client: TestClient):
    """Listing all user credits is admin-only."""
    response = client.get("/api/users/credits")

    assert response.status_code == 401


def test_list_user_credits_for_admin(app, test_engine):
    """Admin users can query credit balances for all users."""
    with Session(test_engine) as db:
        db.execute(
            update(AppUser).where(AppUser.auth0_user_id == "auth0|test-user").values(credit=25)
        )
        db.add(
            AppUser(
                id=uuid4(),
                auth0_user_id="auth0|credit-test-user",
                name="Credit Test User",
                email="credit-test@example.com",
                credit=75,
            )
        )
        db.commit()

    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    try:
        with TestClient(app) as admin_client:
            response = admin_client.get("/api/users/credits")
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert data["users"] == [
        {
            "auth0UserId": "auth0|credit-test-user",
            "name": "Credit Test User",
            "email": "credit-test@example.com",
            "credit": 75,
        },
        {
            "auth0UserId": "auth0|test-user",
            "name": "Test User",
            "email": "test@example.com",
            "credit": 25,
        },
    ]
