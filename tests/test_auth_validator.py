"""Tests for Auth0 JWT validator helpers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from jose.exceptions import JWTError

from app.auth import validator


@pytest.fixture(autouse=True)
def _clear_key_cache():
    validator.KEY_CACHE.clear()
    yield
    validator.KEY_CACHE.clear()


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH0_DOMAIN", "dev.login.aai.test.biocommons.org.au")
    monkeypatch.setenv("AUTH0_AUDIENCE", "https://api.example.test")


def test_get_auth0_settings_success(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AUTH0_ISSUER", "https://issuer.example/")
    monkeypatch.setenv("AUTH0_ALGORITHMS", "RS256, ES256")

    settings = validator._get_auth0_settings()

    assert settings.domain == "dev.login.aai.test.biocommons.org.au"
    assert settings.audience == "https://api.example.test"
    assert settings.issuer == "https://issuer.example/"
    assert settings.algorithms == ("RS256", "ES256")


def test_get_auth0_settings_uses_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("AUTH0_AUDIENCE", raising=False)

    settings = validator._get_auth0_settings()
    assert settings.domain == "dev.login.aai.test.biocommons.org.au"
    assert settings.audience == "https://dev.api.aai.test.biocommons.org.au"


def test_get_auth0_settings_empty_algorithms(monkeypatch: pytest.MonkeyPatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AUTH0_ALGORITHMS", " , ")

    with pytest.raises(HTTPException) as exc:
        validator._get_auth0_settings()

    assert exc.value.status_code == 500


def test_fetch_rsa_keys_uses_cache(mocker):
    response = mocker.Mock()
    response.json.return_value = {"keys": [{"kid": "k1"}]}
    response.raise_for_status.return_value = None
    get_mock = mocker.patch("app.auth.validator.httpx.get", return_value=response)

    first = validator._fetch_rsa_keys("tenant.example")
    second = validator._fetch_rsa_keys("tenant.example")

    assert first == second
    assert get_mock.call_count == 1


def test_get_rsa_key_found(mocker):
    settings = validator.Auth0Settings("tenant.example", "aud", ("RS256",))
    mocker.patch(
        "app.auth.validator._fetch_rsa_keys",
        return_value={"keys": [{"kid": "kid-1", "kty": "RSA"}]},
    )
    mocker.patch("app.auth.validator.jwt.get_unverified_header", return_value={"kid": "kid-1"})
    expected_key = mocker.Mock()
    mocker.patch("app.auth.validator.jwk.construct", return_value=expected_key)

    key = validator._get_rsa_key("token", settings)

    assert key is expected_key


def test_get_rsa_key_retries_once_and_returns_none(mocker):
    settings = validator.Auth0Settings("tenant.example", "aud", ("RS256",))
    fetch_mock = mocker.patch(
        "app.auth.validator._fetch_rsa_keys",
        return_value={"keys": [{"kid": "other-kid"}]},
    )
    mocker.patch("app.auth.validator.jwt.get_unverified_header", return_value={"kid": "kid-1"})

    key = validator._get_rsa_key("token", settings)

    assert key is None
    assert fetch_mock.call_count == 2


def test_verify_access_token_sub_success(monkeypatch: pytest.MonkeyPatch, mocker):
    _set_required_env(monkeypatch)
    mocker.patch("app.auth.validator._get_rsa_key", return_value=mocker.Mock())
    mocker.patch(
        "app.auth.validator.jwt.decode",
        return_value={"sub": "auth0|abc123"},
    )

    assert validator.verify_access_token_sub("token") == "auth0|abc123"


def test_verify_access_token_sub_invalid_header(monkeypatch: pytest.MonkeyPatch, mocker):
    _set_required_env(monkeypatch)
    mocker.patch("app.auth.validator._get_rsa_key", side_effect=JWTError("bad header"))

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub("token")

    assert exc.value.status_code == 401


def test_verify_access_token_sub_http_error(monkeypatch: pytest.MonkeyPatch, mocker):
    _set_required_env(monkeypatch)
    request = httpx.Request("GET", "https://tenant/.well-known/jwks.json")
    mocker.patch(
        "app.auth.validator._get_rsa_key",
        side_effect=httpx.RequestError("boom", request=request),
    )

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub("token")

    assert exc.value.status_code == 401


def test_verify_access_token_sub_missing_signing_key(monkeypatch: pytest.MonkeyPatch, mocker):
    _set_required_env(monkeypatch)
    mocker.patch("app.auth.validator._get_rsa_key", return_value=None)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub("token")

    assert exc.value.status_code == 401


def test_verify_access_token_sub_invalid_payload(monkeypatch: pytest.MonkeyPatch, mocker):
    _set_required_env(monkeypatch)
    mocker.patch("app.auth.validator._get_rsa_key", return_value=mocker.Mock())
    mocker.patch("app.auth.validator.jwt.decode", side_effect=JWTError("bad payload"))

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub("token")

    assert exc.value.status_code == 401


@pytest.mark.parametrize("payload", [{}, {"sub": ""}, {"sub": 123}])
def test_verify_access_token_sub_missing_subject(
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mocker,
):
    _set_required_env(monkeypatch)
    mocker.patch("app.auth.validator._get_rsa_key", return_value=mocker.Mock())
    mocker.patch("app.auth.validator.jwt.decode", return_value=payload)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub("token")

    assert exc.value.status_code == 401
