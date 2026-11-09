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