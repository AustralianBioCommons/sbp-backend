"""Globus Transfer client construction (service-account / client-credentials auth)."""

from __future__ import annotations

import globus_sdk
from globus_sdk.token_storage import MemoryTokenStorage

from ..config import GlobusSettings


def get_transfer_client(globus_settings: GlobusSettings) -> globus_sdk.TransferClient:
    """
    Return an authenticated Globus TransferClient for the SBP service account.
    """
    app = globus_sdk.ClientApp(
        "SBP Service Account",
        client_id=globus_settings.client_id,
        client_secret=globus_settings.client_secret,
        config=globus_sdk.GlobusAppConfig(token_storage=MemoryTokenStorage()),
    )
    transfer_client = globus_sdk.TransferClient(app=app)
    # Only the backend collection's data_access scope is requested here
    transfer_client.add_app_data_access_scope(globus_settings.backend_collection_id)
    return transfer_client
