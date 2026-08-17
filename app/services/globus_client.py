"""Globus Transfer client construction (service-account / client-credentials auth)."""

from __future__ import annotations

import globus_sdk
from globus_sdk.token_storage import MemoryTokenStorage

from ..config import GlobusSettings, get_settings


def get_transfer_client(globus_settings: GlobusSettings | None = None) -> globus_sdk.TransferClient:
    """
    Return an authenticated Globus TransferClient for the SBP service account.
    """
    globus_settings = globus_settings or get_settings().globus
    app = globus_sdk.ClientApp(
        "SBP Service Account",
        client_id=globus_settings.client_id,
        client_secret=globus_settings.client_secret,
        config=globus_sdk.GlobusAppConfig(token_storage=MemoryTokenStorage()),
    )
    transfer_client = globus_sdk.TransferClient(app=app)
    # Only GLOBUS_GADI_COLLECTION_ID's data_access scope is requested here. Verified against
    # this environment's actual collections: requesting it for GLOBUS_S3_COLLECTION_ID too
    # makes Globus Auth reject the *entire* token request with UNKNOWN_SCOPE_ERROR -
    # this S3 collection doesn't have (or doesn't need) a data_access scope of its
    # own. If GLOBUS_S3_COLLECTION_ID is ever repointed at a collection that does need one,
    # add it back here (and confirm the client has been granted access on it first).
    transfer_client.add_app_data_access_scope(globus_settings.gadi_collection_id)
    return transfer_client
