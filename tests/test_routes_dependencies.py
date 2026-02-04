"""Coverage tests for route dependencies."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(None, _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_invalid_authorization_header():
    with pytest.raises(HTTPException) as exc:
        get_current_user_id("Token abc", _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_unknown_user(mocker: MockerFixture):
    mocker.patch("app.routes.dependencies.verify_access_token_sub", return_value="auth0|x")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id("Bearer mock-token", _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_success(mocker: MockerFixture):
    mocker.patch("app.routes.dependencies.verify_access_token_sub", return_value="auth0|x")
    user = SimpleNamespace(id="u-1")
    assert get_current_user_id("Bearer mock-token", _DB(user)) == "u-1"
