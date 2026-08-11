"""Globus Transfer client construction (service-account / client-credentials auth)."""

from __future__ import annotations

import os
from functools import lru_cache

import globus_sdk
from globus_sdk.token_storage import MemoryTokenStorage

from .globus_errors import GlobusConfigurationError


def _get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise GlobusConfigurationError(f"Missing required environment variable: {key}")
    return value


@lru_cache(maxsize=1)
def get_transfer_client() -> globus_sdk.TransferClient:
    """
    Build and cache an authenticated Globus TransferClient for the SBP service account.
    """
    client_id = _get_required_env("GLOBUS_CLIENT_ID")
    client_secret = _get_required_env("GLOBUS_CLIENT_SECRET")
    gadi_collection_id = _get_required_env("GADI_COLLECTION_ID")

    app = globus_sdk.ClientApp(
        "SBP Service Account",
        client_id=client_id,
        client_secret=client_secret,
        config=globus_sdk.GlobusAppConfig(token_storage=MemoryTokenStorage()),
    )
    transfer_client = globus_sdk.TransferClient(app=app)
    # Only GADI_COLLECTION_ID's data_access scope is requested here. Verified against
    # this environment's actual collections: requesting it for S3_COLLECTION_ID too
    # makes Globus Auth reject the *entire* token request with UNKNOWN_SCOPE_ERROR -
    # this S3 collection doesn't have (or doesn't need) a data_access scope of its
    # own. If S3_COLLECTION_ID is ever repointed at a collection that does need one,
    # add it back here (and confirm the client has been granted access on it first).
    transfer_client.add_app_data_access_scope(gadi_collection_id)
    return transfer_client
