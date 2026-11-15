from enum import StrEnum


class ComputeBackendName(StrEnum):
    GADI = "gadi"
    SETONIX = "setonix"


class RepositoryTransferMode(StrEnum):
    NATIVE = "native"
    GLOBUS = "globus"


class DataTransferMode(StrEnum):
    NATIVE = "native"
    GLOBUS = "globus"


class RepositoryStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class BackendSelectionMethod(StrEnum):
    """
    How to select backends for jobs. Currently happens
    at submission time, since we need to prepare the input
    and launch payload in advance.
    """
    RANDOM = "random"
    CYCLE = "cycle"
