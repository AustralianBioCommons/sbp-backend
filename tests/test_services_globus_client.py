"""Tests for the Globus Transfer client construction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import GlobusSettings
from app.services.globus_client import get_transfer_client


def _globus_settings() -> GlobusSettings:
    return GlobusSettings(
        client_id="the-client-id",
        client_secret="the-client-secret",
        gadi_collection_id="the-gadi-collection-id",
        s3_collection_id="the-s3-collection-id",
        gadi_collection_root="/test",
        input_dir="/test/input",
        output_dir="/test/output",
    )


def test_get_transfer_client_builds_client_app_from_settings():
    globus_settings = _globus_settings()

    mock_app = MagicMock()
    mock_transfer_client = MagicMock()
    with (
        patch(
            "app.services.globus_client.globus_sdk.ClientApp", return_value=mock_app
        ) as mock_app_cls,
        patch(
            "app.services.globus_client.globus_sdk.TransferClient",
            return_value=mock_transfer_client,
        ) as mock_transfer_cls,
    ):
        result = get_transfer_client(globus_settings)

    mock_app_cls.assert_called_once()
    call_args, call_kwargs = mock_app_cls.call_args
    assert call_kwargs["client_id"] == "the-client-id"
    assert call_kwargs["client_secret"] == "the-client-secret"

    mock_transfer_cls.assert_called_once_with(app=mock_app)
    assert result is mock_transfer_client


def test_get_transfer_client_only_requests_data_access_scope_for_gadi_collection():
    """GLOBUS_S3_COLLECTION_ID must NOT be passed to add_app_data_access_scope - doing so
    breaks the token request entirely for a collection that doesn't have/need its
    own data_access scope (see the comment in globus_client.py)."""
    globus_settings = _globus_settings()

    mock_transfer_client = MagicMock()
    with (
        patch("app.services.globus_client.globus_sdk.ClientApp"),
        patch(
            "app.services.globus_client.globus_sdk.TransferClient",
            return_value=mock_transfer_client,
        ),
    ):
        get_transfer_client(globus_settings)

    mock_transfer_client.add_app_data_access_scope.assert_called_once_with("the-gadi-collection-id")


def test_get_transfer_client_uses_get_settings_when_settings_not_provided():
    globus_settings = _globus_settings()

    mock_app = MagicMock()
    mock_transfer_client = MagicMock()
    with (
        patch(
            "app.services.globus_client.get_settings",
            return_value=SimpleNamespace(globus=globus_settings),
        ),
        patch("app.services.globus_client.globus_sdk.ClientApp", return_value=mock_app) as mock_app_cls,
        patch(
            "app.services.globus_client.globus_sdk.TransferClient",
            return_value=mock_transfer_client,
        ),
    ):
        result = get_transfer_client()

    mock_app_cls.assert_called_once()
    assert mock_app_cls.call_args.kwargs["client_id"] == "the-client-id"
    assert result is mock_transfer_client
