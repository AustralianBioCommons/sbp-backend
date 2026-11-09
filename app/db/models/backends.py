from datetime import datetime, UTC
from enum import StrEnum
from typing import Literal
from uuid import uuid7

from sqlalchemy import UUID, Text, Boolean, String, Integer, DateTime, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from .. import Base


class ComputeBackendName(StrEnum):
    GADI = "gadi"
    SETONIX = "setonix"


class RepositoryTransferMode(StrEnum):
    NATIVE = "native"
    GLOBUS = "globus"


class DataTransferMode(StrEnum):
    NATIVE = "native"
    GLOBUS = "globus"


class ComputeBackend(Base):
    __tablename__ = 'compute_backend'
    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, defaul=uuid7)
    name: Mapped[ComputeBackendName] = mapped_column(
        SAEnum(
            ComputeBackendName,
            name="compute_backend_name",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True
        ),
        unique=True,
        nullable=False,
    )
    label: Mapped[str] = mapped_column(Text, default=None, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seqera_workspace_id: Mapped[str] = mapped_column(String(length=100), nullable=False)
    seqera_compute_env_id: Mapped[str] = mapped_column(String(length=100), nullable=False)
    # Prefix used for backend-specific environment variables
    env_prefix: Mapped[str] = mapped_column(String(length=32), nullable=False)
    # Base work directory (file path) on the backend
    base_work_dir: Mapped[str] = mapped_column(String(length=256), nullable=False)
    # Mode for transferring workflows - Globus or native
    repository_transfer: Mapped[RepositoryTransferMode] = mapped_column(
        SAEnum(
            RepositoryTransferMode,
            name="repo_transfer_mode",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True
        ),
        default=RepositoryTransferMode.NATIVE,
        nullable=False,
    )
    # Mode for transferring input/output files - Globus or native
    data_transfer: Mapped[DataTransferMode] = mapped_column(
        SAEnum(
            DataTransferMode,
            name="data_transfer_mode",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True
        )
    )
    # Limit on concurrent workflows - None if no limit
    max_workflows: Mapped[int | None] = mapped_column(Integer, default=None, nullable=True)
    # Service usage tracking enabled for the current backend - each backend
    #   requires custom config for this (e.g. Nextflow plugin)
    service_usage_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )



