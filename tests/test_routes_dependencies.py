"""Coverage tests for route dependencies."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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


def test_get_current_user_id_unknown_user():
    with pytest.raises(HTTPException) as exc:
        get_current_user_id("auth0|x", _DB(None))
    assert exc.value.status_code == 401


def test_get_current_user_id_success():
    user = SimpleNamespace(id="u-1")
    assert get_current_user_id("auth0|x", _DB(user)) == "u-1"
