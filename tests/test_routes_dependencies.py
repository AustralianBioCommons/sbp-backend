"""Coverage tests for route dependencies."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pytest_mock import MockerFixture
from sqlalchemy.exc import IntegrityError

from app.routes.dependencies import get_current_user_id


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, value):
        self.value = value
        self.added = []
        self.committed = False
        self.rolled_back = False

    def execute(self, *_args, **_kwargs):
        return _Result(self.value)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_get_current_user_id_missing_header():
    # HTTPBearer will automatically raise 403 for missing credentials
    # So we test with empty credentials
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials, _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_unknown_user_auto_creates(mocker: MockerFixture):
    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={"sub": "auth0|x", "name": "Test User", "email": "Test@Example.com"},
    )
    mocker.patch("app.routes.dependencies.fetch_userinfo_claims", return_value={})
    db = _DB(None)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    created_user_id = get_current_user_id(credentials, db)
    assert isinstance(created_user_id, UUID)
    assert db.committed is True
    assert len(db.added) == 1
    created_user = db.added[0]
    assert created_user.auth0_user_id == "auth0|x"
    assert created_user.name == "Test User"
    assert created_user.email == "test@example.com"


def test_get_current_user_id_success(mocker: MockerFixture):
    mocker.patch("app.routes.dependencies.verify_access_token_claims", return_value={"sub": "auth0|x"})
    mocker.patch("app.routes.dependencies.fetch_userinfo_claims", return_value={})
    user = SimpleNamespace(id="u-1", name="Existing User", email="existing@example.com")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    assert get_current_user_id(credentials, _DB(user)) == "u-1"


def test_get_current_user_id_unknown_user_without_email_uses_fallback(mocker: MockerFixture):
    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={"sub": "auth0|no-email"},
    )
    mocker.patch("app.routes.dependencies.fetch_userinfo_claims", return_value={})
    db = _DB(None)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    _ = get_current_user_id(credentials, db)
    created_user = db.added[0]
    assert created_user.email == "auth0_no-email@unknown.local"


def test_get_current_user_id_race_conflict_fetches_existing(mocker: MockerFixture):
    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={"sub": "auth0|x", "name": "Test User", "email": "test@example.com"},
    )
    mocker.patch("app.routes.dependencies.fetch_userinfo_claims", return_value={})
    existing = SimpleNamespace(id="u-existing")
    db = _DB(None)

    def _raise_conflict():
        db.value = existing
        raise IntegrityError("insert", {}, Exception("conflict"))

    db.commit = _raise_conflict
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    assert get_current_user_id(credentials, db) == "u-existing"
    assert db.rolled_back is True


def test_get_current_user_id_fetches_userinfo_when_claims_missing(mocker: MockerFixture):
    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={"sub": "auth0|x"},
    )
    mocker.patch(
        "app.routes.dependencies.fetch_userinfo_claims",
        return_value={"name": "From UserInfo", "email": "userinfo@example.com"},
    )
    db = _DB(None)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    _ = get_current_user_id(credentials, db)
    created_user = db.added[0]
    assert created_user.name == "From UserInfo"
    assert created_user.email == "userinfo@example.com"
