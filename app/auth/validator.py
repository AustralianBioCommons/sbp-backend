"""Auth0 JWT validation helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

import httpx
from cachetools import TTLCache  # type: ignore[import-untyped]
from fastapi import HTTPException, status
from jose import jwk, jwt
from jose.exceptions import JWTError

KEY_CACHE = TTLCache(maxsize=10, ttl=30 * 60)
DEFAULT_AUTH0_DOMAIN = "dev.login.aai.test.biocommons.org.au"
DEFAULT_AUTH0_AUDIENCE = "https://dev.api.aai.test.biocommons.org.au"


@dataclass(frozen=True)
class Auth0Settings:
    domain: str
    audience: str
    algorithms: tuple[str, ...]
    issuer: str | None = None


def _get_auth0_settings() -> Auth0Settings:
    domain = os.getenv("AUTH0_DOMAIN") or DEFAULT_AUTH0_DOMAIN
    audience = os.getenv("AUTH0_AUDIENCE") or DEFAULT_AUTH0_AUDIENCE
    issuer = os.getenv("AUTH0_ISSUER")
    algorithms_raw = os.getenv("AUTH0_ALGORITHMS", "RS256")
    algorithms = tuple(alg.strip() for alg in algorithms_raw.split(",") if alg.strip())

    if not algorithms:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth configuration error: AUTH0_ALGORITHMS must not be empty",
        )

    return Auth0Settings(
        domain=domain,
        audience=audience,
        algorithms=algorithms,
        issuer=issuer,
    )


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
    settings: Auth0Settings,
    *,
    retry_on_failure: bool = True,
) -> jwk.Key | None:
    jwks = _fetch_rsa_keys(settings.domain)
    unverified_header = jwt.get_unverified_header(token)

    for key in jwks.get("keys", []):
        if key.get("kid") == unverified_header.get("kid"):
            return jwk.construct(key)

    # Retry once with a cold cache to handle key rotation.
    if retry_on_failure:
        KEY_CACHE.clear()
        return _get_rsa_key(token, settings, retry_on_failure=False)

    return None


def verify_access_token_sub(token: str) -> str:
    """Verify Auth0 JWT and return subject claim used as app_users.auth0_user_id."""
    settings = _get_auth0_settings()
    try:
        rsa_key = _get_rsa_key(token, settings=settings)
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

    issuers = [f"https://{settings.domain}/"]
    if settings.issuer:
        issuers.append(settings.issuer)

    try:
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=list(settings.algorithms),
            audience=settings.audience,
            issuer=issuers,
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject claim",
        )
    return subject
