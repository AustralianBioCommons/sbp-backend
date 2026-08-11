"""Error types for Globus data-staging operations."""

from __future__ import annotations


class GlobusConfigurationError(RuntimeError):
    """Raised when required Globus configuration is missing."""


class GlobusTransferError(RuntimeError):
    """Raised when a Globus transfer submission or status check fails."""
