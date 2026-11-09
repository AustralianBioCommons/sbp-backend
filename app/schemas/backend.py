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
