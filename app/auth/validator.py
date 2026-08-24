"""Auth0 JWT validation helpers."""

from __future__ import annotations

from typing import Any, cast

import httpx
from cachetools import TTLCache  # type: ignore[import-untyped]
from fastapi import HTTPException, status
from jose import jwk, jwt
from jose.exceptions import JWTError

from ..config import AuthSettings, Settings, get_settings

KEY_CACHE = TTLCache(maxsize=10, ttl=30 * 60)


def _fetch_rsa_keys(auth0_domain: str) -> dict[str, Any]:
    cache_key = f"jwks_{auth0_domain}"
    if cache_key in KEY_CACHE:
        return cast(dict[str, Any], KEY_CACHE[cache_key])

    jwks_url = f"https://{auth0_domain}/.well-known/jwks.json"
    response = httpx.get(jwks_url, timeout=10)
    response.raise_for_status()
    keys = cast(dict[str, Any], response.json())
    KEY_CACHE[cache_key] = keys
    return keys


def _get_rsa_key(
    token: str,
    auth_settings: AuthSettings,
    *,
    retry_on_failure: bool = True,
) -> jwk.Key | None:
    jwks = _fetch_rsa_keys(auth_settings.domain)
    unverified_header = jwt.get_unverified_header(token)

    for key in jwks.get("keys", []):
        if key.get("kid") == unverified_header.get("kid"):
            return jwk.construct(key)

    # Retry once with a cold cache to handle key rotation.
    if retry_on_failure:
        KEY_CACHE.clear()
        return _get_rsa_key(token, auth_settings, retry_on_failure=False)

    return None


def verify_access_token_sub(token: str, settings: Settings) -> str:
    """Verify Auth0 JWT and return subject claim used as app_users.auth0_user_id."""
    payload = verify_access_token_claims(token, settings)
    return cast(str, payload["sub"])


def verify_access_token_claims(token: str, settings: Settings) -> dict[str, Any]:
    """Verify Auth0 JWT and return decoded claims payload."""
    try:
        rsa_key = _get_rsa_key(token, auth_settings=settings.auth)
    except (JWTError, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    if rsa_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Couldn't find a matching signing key.",
        )

    issuers = [f"https://{settings.auth.domain}/"]
    if settings.auth.issuer:
        issuers.append(settings.auth.issuer)

    try:
        decoded = jwt.decode(
            token,
            rsa_key,
            algorithms=list(settings.auth.algorithms),
            audience=settings.auth.audience,
            issuer=issuers,
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: malformed payload",
        )
    payload = cast(dict[str, Any], decoded)

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject claim",
        )
    return payload


def fetch_userinfo_claims(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch Auth0 /userinfo claims for the provided access token."""
    if settings is None:
        settings = get_settings()
    userinfo_url = f"https://{settings.auth.domain}/userinfo"
    try:
        response = httpx.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return {}

    payload = response.json()
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    return {}
