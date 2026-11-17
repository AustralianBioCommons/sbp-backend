from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from ..config import GlobusSettings


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


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BackendLaunchConfig(BaseModel):
    backend_id: UUID
    workflow_id: UUID
    backend_name: ComputeBackendName
    workspace_id: NonEmptyStr
    compute_env_id: NonEmptyStr
    base_work_dir: NonEmptyStr
    config_path: NonEmptyStr
    ref_database_dir: NonEmptyStr | None = None
    project_id: NonEmptyStr | None = None
    data_transfer: DataTransferMode
    repository_transfer: RepositoryTransferMode
    globus: GlobusSettings | None = None

    def require_globus(self) -> GlobusSettings:
        if self.globus is None:
            raise ValueError("Globus settings are required by the current input/output preparation")
        return self.globus
