"""Error types for Seqera service operations."""


class SeqeraConfigurationError(RuntimeError):
    """Raised when required Seqera configuration is missing."""


class SeqeraAPIError(RuntimeError):
    """Raised when Seqera API calls fail."""
