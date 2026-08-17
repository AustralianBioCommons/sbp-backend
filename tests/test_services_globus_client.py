"""Tests for the Globus Transfer client construction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.globus_client import _get_required_env, get_transfer_client
from app.services.globus_errors import GlobusConfigurationError


@pytest.fixture(autouse=True)
def _clear_transfer_client_cache():
    """get_transfer_client is lru_cache'd - clear it so tests don't leak state."""
    get_transfer_client.cache_clear()
    yield
    get_transfer_client.cache_clear()


def test_get_required_env_missing_raises_configuration_error(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_VAR", raising=False)
    with pytest.raises(GlobusConfigurationError, match="SOME_MISSING_VAR"):
        _get_required_env("SOME_MISSING_VAR")


def test_get_required_env_empty_string_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("SOME_EMPTY_VAR", "")
    with pytest.raises(GlobusConfigurationError, match="SOME_EMPTY_VAR"):
        _get_required_env("SOME_EMPTY_VAR")


def test_get_required_env_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "some-value")
    assert _get_required_env("SOME_VAR") == "some-value"


def test_get_transfer_client_missing_client_id_raises(monkeypatch):
    monkeypatch.delenv("GLOBUS_CLIENT_ID", raising=False)
    with pytest.raises(GlobusConfigurationError, match="GLOBUS_CLIENT_ID"):
        get_transfer_client()


def test_get_transfer_client_missing_client_secret_raises(monkeypatch):
    monkeypatch.setenv("GLOBUS_CLIENT_ID", "client-id")
    monkeypatch.delenv("GLOBUS_CLIENT_SECRET", raising=False)
    with pytest.raises(GlobusConfigurationError, match="GLOBUS_CLIENT_SECRET"):
        get_transfer_client()


def test_get_transfer_client_missing_gadi_collection_id_raises(monkeypatch):
    monkeypatch.setenv("GLOBUS_CLIENT_ID", "client-id")
    monkeypatch.setenv("GLOBUS_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("GADI_COLLECTION_ID", raising=False)
    with pytest.raises(GlobusConfigurationError, match="GADI_COLLECTION_ID"):
        get_transfer_client()


def test_get_transfer_client_builds_client_app_from_env(monkeypatch):
    monkeypatch.setenv("GLOBUS_CLIENT_ID", "the-client-id")
    monkeypatch.setenv("GLOBUS_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("GADI_COLLECTION_ID", "the-gadi-collection-id")

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
        result = get_transfer_client()

    mock_app_cls.assert_called_once()
    call_args, call_kwargs = mock_app_cls.call_args
    assert call_kwargs["client_id"] == "the-client-id"
    assert call_kwargs["client_secret"] == "the-client-secret"

    mock_transfer_cls.assert_called_once_with(app=mock_app)
    assert result is mock_transfer_client


def test_get_transfer_client_only_requests_data_access_scope_for_gadi_collection(monkeypatch):
    """S3_COLLECTION_ID must NOT be passed to add_app_data_access_scope - doing so
    breaks the token request entirely for a collection that doesn't have/need its
    own data_access scope (see the comment in globus_client.py)."""
    monkeypatch.setenv("GLOBUS_CLIENT_ID", "the-client-id")
    monkeypatch.setenv("GLOBUS_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("GADI_COLLECTION_ID", "the-gadi-collection-id")
    monkeypatch.setenv("S3_COLLECTION_ID", "the-s3-collection-id")

    mock_transfer_client = MagicMock()
    with (
        patch("app.services.globus_client.globus_sdk.ClientApp"),
        patch(
            "app.services.globus_client.globus_sdk.TransferClient",
            return_value=mock_transfer_client,
        ),
    ):
        get_transfer_client()

    mock_transfer_client.add_app_data_access_scope.assert_called_once_with("the-gadi-collection-id")


def test_get_transfer_client_is_cached(monkeypatch):
    monkeypatch.setenv("GLOBUS_CLIENT_ID", "the-client-id")
    monkeypatch.setenv("GLOBUS_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("GADI_COLLECTION_ID", "the-gadi-collection-id")

    with (
        patch("app.services.globus_client.globus_sdk.ClientApp") as mock_app_cls,
        patch("app.services.globus_client.globus_sdk.TransferClient") as mock_transfer_cls,
    ):
        first = get_transfer_client()
        second = get_transfer_client()

    assert first is second
    mock_app_cls.assert_called_once()
    mock_transfer_cls.assert_called_once()
