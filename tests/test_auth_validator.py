"""Tests for Auth0 JWT validator helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import HTTPException
from jose import jwt
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


def generate_public_private_key_pair() -> tuple[RSAPublicKey, RSAPrivateKey]:
    """Generate a public/private RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return public_key, private_key


@dataclass
class AuthTokenData:
    """Stores all the information needed to generate an access token and test decoding."""

    private_key: RSAPrivateKey
    public_key: RSAPublicKey
    access_token_str: str
    access_token_data: dict
    key_id: str


def create_access_token(
    sub: str | None = None,
    iss: str = "https://dev.login.aai.test.biocommons.org.au/",
    aud: str = "https://api.example.test",
    iat: int | None = None,
    exp: int | None = None,
    algorithm: str = "RS256",
    public_key_id: str = "test-key-id",
) -> AuthTokenData:
    """Create a JWT access token with a dummy private/public key for signing."""
    if sub is None:
        sub = f"auth0|{uuid.uuid4().hex}"
    if iat is None:
        iat = int(datetime.now().timestamp())
    if exp is None:
        exp = int((datetime.now() + timedelta(hours=1)).timestamp())

    payload = {
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "iat": iat,
        "exp": exp,
    }

    public_key, private_key = generate_public_private_key_pair()

    from cryptography.hazmat.primitives import serialization

    pem_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    access_token_encoded = jwt.encode(
        payload,
        key=pem_private_key,
        algorithm=algorithm,
        headers={"kid": public_key_id},
    )

    return AuthTokenData(
        private_key=private_key,
        public_key=public_key,
        access_token_str=access_token_encoded,
        access_token_data=payload,
        key_id=public_key_id,
    )


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
    """Test JWT validation with actual token instead of mocking decode."""
    _set_required_env(monkeypatch)

    token = create_access_token(
        sub="auth0|abc123",
        iss="https://dev.login.aai.test.biocommons.org.au/",
        aud="https://api.example.test",
    )

    # Mock the key retrieval to return our test public key
    mocker.patch("app.auth.validator._get_rsa_key", return_value=token.public_key)

    result = validator.verify_access_token_sub(token.access_token_str)
    assert result == "auth0|abc123"


def test_verify_access_token_sub_with_custom_issuer(monkeypatch: pytest.MonkeyPatch, mocker):
    """Test JWT validation with custom issuer setting."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AUTH0_ISSUER", "https://custom.example.com/")

    token = create_access_token(
        sub="auth0|xyz789",
        iss="https://custom.example.com/",
        aud="https://api.example.test",
    )

    mocker.patch("app.auth.validator._get_rsa_key", return_value=token.public_key)

    result = validator.verify_access_token_sub(token.access_token_str)
    assert result == "auth0|xyz789"


def test_verify_access_token_sub_expired_token(monkeypatch: pytest.MonkeyPatch, mocker):
    """Test JWT validation fails with expired token."""
    _set_required_env(monkeypatch)

    token = create_access_token(
        sub="auth0|expired",
        iss="https://dev.login.aai.test.biocommons.org.au/",
        aud="https://api.example.test",
        exp=int((datetime.now() - timedelta(hours=1)).timestamp()),
    )

    mocker.patch("app.auth.validator._get_rsa_key", return_value=token.public_key)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub(token.access_token_str)

    assert exc.value.status_code == 401
    assert "expired" in str(exc.value.detail).lower()


def test_verify_access_token_sub_wrong_audience(monkeypatch: pytest.MonkeyPatch, mocker):
    """Test JWT validation fails with wrong audience."""
    _set_required_env(monkeypatch)

    token = create_access_token(
        sub="auth0|wrongaud",
        iss="https://dev.login.aai.test.biocommons.org.au/",
        aud="https://wrong.audience.com",
    )

    mocker.patch("app.auth.validator._get_rsa_key", return_value=token.public_key)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub(token.access_token_str)

    assert exc.value.status_code == 401


def test_verify_access_token_sub_wrong_issuer(monkeypatch: pytest.MonkeyPatch, mocker):
    """Test JWT validation fails with wrong issuer."""
    _set_required_env(monkeypatch)

    token = create_access_token(
        sub="auth0|wrongiss",
        iss="https://evil.issuer.com/",
        aud="https://api.example.test",
    )

    mocker.patch("app.auth.validator._get_rsa_key", return_value=token.public_key)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub(token.access_token_str)

    assert exc.value.status_code == 401


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
    """Test JWT validation fails when signature is invalid."""
    _set_required_env(monkeypatch)

    token = create_access_token(
        sub="auth0|test",
        iss="https://dev.login.aai.test.biocommons.org.au/",
        aud="https://api.example.test",
    )

    # Create a different public key to cause signature verification failure
    _, different_key = generate_public_private_key_pair()
    mocker.patch("app.auth.validator._get_rsa_key", return_value=different_key.public_key())

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub(token.access_token_str)

    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "sub_value",
    [None, "", "   ", 123],
    ids=["missing", "empty", "whitespace", "not_string"],
)
def test_verify_access_token_sub_invalid_subject(
    sub_value: Any,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
):
    """Test JWT validation fails when subject claim is invalid or missing."""
    _set_required_env(monkeypatch)

    # Create a token with valid structure but invalid sub claim
    public_key, private_key = generate_public_private_key_pair()

    from cryptography.hazmat.primitives import serialization
    pem_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    payload = {
        "iss": "https://dev.login.aai.test.biocommons.org.au/",
        "aud": "https://api.example.test",
        "iat": int(datetime.now().timestamp()),
        "exp": int((datetime.now() + timedelta(hours=1)).timestamp()),
    }

    if sub_value is not None:
        payload["sub"] = sub_value

    token_str = jwt.encode(
        payload,
        key=pem_private_key,
        algorithm="RS256",
        headers={"kid": "test-key-id"},
    )

    mocker.patch("app.auth.validator._get_rsa_key", return_value=public_key)

    with pytest.raises(HTTPException) as exc:
        validator.verify_access_token_sub(token_str)

    assert exc.value.status_code == 401
    assert "subject" in str(exc.value.detail).lower()
