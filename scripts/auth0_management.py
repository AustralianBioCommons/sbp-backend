"""Minimal Auth0 Management API client, used by usage_report.py to scope
user counts to since the soft launch date via each user's last_login.

AppUser has no signup/login timestamp of its own, so this is the only way to
get that scoping without a DB migration. last_login is tenant-wide across
every biocommons app (AAI portal, SBP, etc.), not SBP-specific, so treat the
since-launch numbers as an approximation, not an exact SBP login count.

Needs a Machine-to-Machine Auth0 application authorized for the Management
API (read:users scope) on the same tenant as the backend's AUTH_DOMAIN -
AUTH_CLIENT_ID/SECRET (the login app) won't work for this. All settings are
optional; if unset, usage_report.py falls back to all-time counts.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 0.5


class Auth0ManagementSettings(BaseSettings):
    domain: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    model_config = SettingsConfigDict(env_prefix="AUTH0_MGMT_", env_file=".env", extra="ignore")

    def is_configured(self) -> bool:
        return bool(self.domain and self.client_id and self.client_secret)


def _get_management_token(settings: Auth0ManagementSettings) -> str:
    response = httpx.post(
        f"https://{settings.domain}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "audience": f"https://{settings.domain}/api/v2/",
        },
        timeout=10,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Auth0 token response missing access_token.")
    return token


def fetch_last_login_by_user_id(
    settings: Auth0ManagementSettings, auth0_user_ids: list[str]
) -> dict[str, datetime]:
    """Look up each user's tenant-wide last_login, batched to stay within
    query-length/rate limits. A user_id Auth0 doesn't return (e.g. deleted,
    or never logged in) is simply absent from the result."""
    if not auth0_user_ids:
        return {}

    token = _get_management_token(settings)
    result: dict[str, datetime] = {}

    with httpx.Client(
        base_url=f"https://{settings.domain}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as client:
        for i in range(0, len(auth0_user_ids), BATCH_SIZE):
            batch = auth0_user_ids[i : i + BATCH_SIZE]
            response = client.get(
                "/api/v2/users",
                params={
                    "q": " OR ".join(f'user_id:"{uid}"' for uid in batch),
                    "search_engine": "v3",
                    "fields": "user_id,last_login",
                    "per_page": BATCH_SIZE,
                },
            )
            response.raise_for_status()
            for user in response.json():
                user_id, last_login = user.get("user_id"), user.get("last_login")
                if user_id and last_login:
                    result[user_id] = datetime.fromisoformat(last_login.replace("Z", "+00:00"))
            if i + BATCH_SIZE < len(auth0_user_ids):
                time.sleep(BATCH_DELAY_SECONDS)

    return result
