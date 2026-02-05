"""Coverage tests for route dependencies."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pytest_mock import MockerFixture

from app.routes.dependencies import get_current_user_id


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, value):
        self.value = value

    def execute(self, *_args, **_kwargs):
        return _Result(self.value)


def test_get_current_user_id_missing_header():
    # HTTPBearer will automatically raise 403 for missing credentials
    # So we test with empty credentials
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials, _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_unknown_user(mocker: MockerFixture):
    mocker.patch("app.routes.dependencies.verify_access_token_sub", return_value="auth0|x")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials, _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_success(mocker: MockerFixture):
    mocker.patch("app.routes.dependencies.verify_access_token_sub", return_value="auth0|x")
    user = SimpleNamespace(id="u-1")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock-token")
    assert get_current_user_id(credentials, _DB(user)) == "u-1"
