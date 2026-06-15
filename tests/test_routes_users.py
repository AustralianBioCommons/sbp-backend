"""Tests for user credit routes."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.admin import require_admin_access
from app.db.models.core import AppUser

TEST_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TEST_AUTH0_USER_ID = "auth0|test-user"


def _credit_url(auth0_user_id: str) -> str:
    return f"/api/users/credits/{quote(auth0_user_id, safe='')}"


def test_get_my_credit(client: TestClient, test_engine):
    """Current user can query their own credit balance."""
    with Session(test_engine) as db:
        db.execute(
            update(AppUser).where(AppUser.auth0_user_id == TEST_AUTH0_USER_ID).values(credit=25)
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
            update(AppUser).where(AppUser.auth0_user_id == TEST_AUTH0_USER_ID).values(credit=25)
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
    assert data["page"] == 1
    assert data["perPage"] == 50
    assert data["users"] == [
        {
            "auth0UserId": "auth0|credit-test-user",
            "name": "Credit Test User",
            "email": "credit-test@example.com",
            "credit": 75,
            "creditUpdatedAt": None,
            "creditUpdatedBy": None,
        },
        {
            "auth0UserId": "auth0|test-user",
            "name": "Test User",
            "email": "test@example.com",
            "credit": 25,
            "creditUpdatedAt": None,
            "creditUpdatedBy": None,
        },
    ]


def test_list_user_credits_translates_page_to_offset(app, test_engine):
    """Backend accepts page/per_page and translates them before querying."""
    with Session(test_engine) as db:
        db.add(
            AppUser(
                id=uuid4(),
                auth0_user_id="auth0|second-page-user",
                name="Second Page User",
                email="z-second-page@example.com",
                credit=10,
            )
        )
        db.commit()

    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    try:
        with TestClient(app) as admin_client:
            response = admin_client.get("/api/users/credits?page=2&per_page=1")
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["page"] == 2
    assert data["perPage"] == 1
    assert data["users"] == [
        {
            "auth0UserId": "auth0|second-page-user",
            "name": "Second Page User",
            "email": "z-second-page@example.com",
            "credit": 10,
            "creditUpdatedAt": None,
            "creditUpdatedBy": None,
        }
    ]


def test_get_user_credit_for_admin(app, test_engine):
    """Admin users can query one user's credit balance."""
    with Session(test_engine) as db:
        db.execute(
            update(AppUser).where(AppUser.auth0_user_id == TEST_AUTH0_USER_ID).values(credit=25)
        )
        db.commit()

    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    try:
        with TestClient(app) as admin_client:
            response = admin_client.get(_credit_url(TEST_AUTH0_USER_ID))
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 200
    assert response.json() == {
        "auth0UserId": TEST_AUTH0_USER_ID,
        "name": "Test User",
        "email": "test@example.com",
        "credit": 25,
        "creditUpdatedAt": None,
        "creditUpdatedBy": None,
    }


def test_update_user_credit_requires_admin(client: TestClient):
    """Updating a user's credit balance is admin-only."""
    response = client.put(_credit_url(TEST_AUTH0_USER_ID), json={"credit": 100})

    assert response.status_code == 401


def test_update_user_credit_for_admin(app, test_engine):
    """Admin users can set an absolute user credit balance."""
    app.dependency_overrides[require_admin_access] = lambda: {
        "sub": "auth0|admin",
        "email": "admin@example.com",
    }
    try:
        with TestClient(app) as admin_client:
            response = admin_client.put(_credit_url(TEST_AUTH0_USER_ID), json={"credit": 100})
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "auth0UserId": TEST_AUTH0_USER_ID,
        "name": "Test User",
        "email": "test@example.com",
        "credit": 100,
        "creditUpdatedAt": data["creditUpdatedAt"],
        "creditUpdatedBy": "admin@example.com",
    }
    assert data["creditUpdatedAt"] is not None
    with Session(test_engine) as db:
        saved = db.execute(
            select(AppUser.credit, AppUser.credit_updated_at, AppUser.credit_updated_by).where(
                AppUser.auth0_user_id == TEST_AUTH0_USER_ID
            )
        ).one()
    assert saved.credit == 100
    assert saved.credit_updated_at is not None
    assert saved.credit_updated_by == "admin@example.com"


def test_update_user_credit_rejects_negative_credit(app):
    """Credit balances cannot be set below zero."""
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    try:
        with TestClient(app) as admin_client:
            response = admin_client.put(_credit_url(TEST_AUTH0_USER_ID), json={"credit": -1})
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 422


def test_update_user_credit_returns_404_for_unknown_user(app):
    """Updating an unknown user returns 404."""
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    try:
        with TestClient(app) as admin_client:
            response = admin_client.put(_credit_url("auth0|missing-user"), json={"credit": 100})
    finally:
        app.dependency_overrides.pop(require_admin_access, None)

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"
