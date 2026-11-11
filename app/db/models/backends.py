from datetime import UTC, datetime
from typing import Self
from uuid import UUID as PyUUID
from uuid import uuid7

from sqlalchemy import (
    UUID,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from ...config import ComputeBackendSettings
from ...schemas.backend import (
    ComputeBackendName,
    DataTransferMode,
    RepositoryStatus,
    RepositoryTransferMode,
)
from .. import Base
from . import core


class ComputeBackend(Base):
    __tablename__ = "compute_backend"
    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[ComputeBackendName] = mapped_column(
        SAEnum(
            ComputeBackendName,
            name="compute_backend_name",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True,
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
    project_id: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    # Mode for transferring workflows - Globus or native
    repository_transfer: Mapped[RepositoryTransferMode] = mapped_column(
        SAEnum(
            RepositoryTransferMode,
            name="repo_transfer_mode",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True,
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
            validate_strings=True,
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
        onupdate=func.now(),
    )

    @classmethod
    def get_by_name(cls, session: Session, name: ComputeBackendName) -> Self | None:
        return session.scalar(select(cls).where(cls.name == name))

    def get_settings(self, env_file: str | None = ".env") -> ComputeBackendSettings:
        use_globus = (
            self.repository_transfer == RepositoryTransferMode.GLOBUS
            or self.data_transfer == DataTransferMode.GLOBUS
        )
        return ComputeBackendSettings.load(
            env_prefix=self.env_prefix,
            globus_enabled=use_globus,
            env_file=env_file,
        )


class BackendWorkflowSettings(Base):
    """
    Backend-specific settings for workflows, e.g.
    disabling a particular workflow for the backend,
    setting specific config
    """

    __tablename__ = "backend_workflow_settings"
    __table_args__ = (UniqueConstraint("compute_backend_id", "workflow_id"),)
    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    compute_backend_id: Mapped[PyUUID] = mapped_column(
        ForeignKey("compute_backend.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[PyUUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_path: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    # Base directory of the reference database
    ref_database_dir: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)

    compute_backend: Mapped[ComputeBackend] = relationship()
    workflow: Mapped[core.Workflow] = relationship(core.Workflow)


class WorkflowRepoStatus(Base):
    __tablename__ = "workflow_repo_status"
    __table_args__ = (UniqueConstraint("compute_backend_id", "workflow_id"),)

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    compute_backend_id: Mapped[PyUUID] = mapped_column(
        ForeignKey("compute_backend.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[PyUUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    staged_commit_sha: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    # File path on the backend
    location: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RepositoryStatus] = mapped_column(
        SAEnum(
            RepositoryStatus,
            name="repository_status",
            values_callable=lambda members: [member.value for member in members],
            validate_strings=True,
        )
    )
    transfer_id: Mapped[str | None] = mapped_column(String(length=48), nullable=True)
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
        onupdate=func.now(),
    )

    compute_backend: Mapped[ComputeBackend] = relationship()
    workflow: Mapped[core.Workflow] = relationship(core.Workflow)
