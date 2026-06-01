"""Error types for Seqera service operations."""

from __future__ import annotations


class SeqeraConfigurationError(RuntimeError):
    """Raised when required Seqera configuration is missing."""


class SeqeraExecutorError(RuntimeError):
    """Raised when a Seqera Platform API call fails at runtime."""


class SeqeraAPIError(RuntimeError):
    """Raised when Seqera API calls fail."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
