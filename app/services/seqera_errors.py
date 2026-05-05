"""Error types for Seqera service operations."""


class SeqeraConfigurationError(RuntimeError):
    """Raised when required Seqera configuration is missing."""


class SeqeraAPIError(RuntimeError):
    """Raised when Seqera API calls fail."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
