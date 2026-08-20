"""Core database models for workflows and run metadata."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID as PyUUID
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from ...schemas.workflows.shared import TERMINAL_SEQERA_STATUSES, PipelineStatus
from .. import Base

_InetType = Text().with_variant(INET(), "postgresql")


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    auth0_user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    credit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    credit_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credit_updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set the first time this user's token carries the SBP workflow-execution
    # role, when the one-time bundle grant is applied. Doubles as the "already
    # granted" flag so the grant never repeats, and as the eligibility filter
    # for the monthly credit refresh (refresh_user_credits), which only resets
    # credit for users who have been through this grant at least once.
    sbp_bundle_credit_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    workflow_runs: Mapped[list[WorkflowRun]] = relationship(back_populates="owner")


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        # At most one row per (name, tool) for workflows with tool-specific
        # configs (e.g. de-novo-design: bindcraft vs rfdiffusion).
        UniqueConstraint("name", "tool"),
        # At most one generic (tool-independent) row per name. sqlite_where is
        # needed alongside postgresql_where so the tests' SQLite schema (built
        # via Base.metadata.create_all) enforces the same partial uniqueness
        # as the Postgres migration; without it, SQLite silently drops the
        # WHERE clause and the index becomes a full unique constraint on name.
        Index(
            "uq_workflows_name_null_tool",
            "name",
            unique=True,
            postgresql_where=text("tool IS NULL"),
            sqlite_where=text("tool IS NULL"),
        ),
    )

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    default_revision: Mapped[str] = mapped_column(Text, nullable=False)
    config_path: Mapped[str] = mapped_column(Text, nullable=False)
    prerun_script_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list[WorkflowRun]] = relationship(back_populates="workflow")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("seqera_run_id"),
        UniqueConstraint("work_dir"),
    )

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    workflow_id: Mapped[PyUUID | None] = mapped_column(ForeignKey("workflows.id"))
    owner_user_id: Mapped[PyUUID] = mapped_column(ForeignKey("app_users.id"), nullable=False)
    seqera_run_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    binder_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_form_data: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    work_dir: Mapped[str] = mapped_column(Text, nullable=False)
    launch_ip: Mapped[str | None] = mapped_column(_InetType, nullable=True)
    submission_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tool: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_usage: Mapped[float | None] = mapped_column(Float, nullable=True)
    seqera_final_status: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    sync_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[AppUser] = relationship(back_populates="workflow_runs")
    workflow: Mapped[Workflow | None] = relationship(back_populates="runs")
    metrics: Mapped[RunMetric | None] = relationship(back_populates="run", uselist=False)
    inputs: Mapped[list[RunInput]] = relationship(back_populates="run")
    outputs: Mapped[list[RunOutput]] = relationship(back_populates="run")
    data_transfers: Mapped[list[DataTransfer]] = relationship(back_populates="workflow_run")

    def get_queued_job(self, session: Session):
        """Get the latest queued job for this workflow run."""
        from app.db.models.job_queue import QueuedJob

        return (
            session.query(QueuedJob)
            .filter(QueuedJob.workflow_run_id == self.id)
            .order_by(QueuedJob.queued_at.desc())
            .first()
        )

    def is_fully_synced(self) -> bool:
        seqera_complete = self.is_seqera_finalized()
        sync_complete = self.sync_completed_at is not None
        return seqera_complete and sync_complete

    def is_seqera_finalized(self) -> bool:
        if self.seqera_final_status is None:
            return False
        return self.seqera_final_status.upper() in TERMINAL_SEQERA_STATUSES

    def is_syncing_results(self) -> bool:
        if self.seqera_final_status is None:
            return False
        return (
            self.seqera_final_status == PipelineStatus.SUCCEEDED.value
            and self.sync_completed_at is None
        )

    @property
    def results_sync_status(self):
        """
        Simple sync status to report to frontend
        """
        if self.sync_completed_at is None:
            return "syncing"
        else:
            return "ready"


class S3Object(Base):
    __tablename__ = "s3_objects"
    __table_args__ = (UniqueConstraint("URI"),)

    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    uri: Mapped[str] = mapped_column("URI", Text, nullable=False)
    version_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    run_inputs: Mapped[list[RunInput]] = relationship(back_populates="s3_object")
    run_outputs: Mapped[list[RunOutput]] = relationship(back_populates="s3_object")


class RunInput(Base):
    __tablename__ = "run_inputs"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "s3_object_id"),
        UniqueConstraint("data_transfer_id"),
    )

    run_id: Mapped[PyUUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)
    data_transfer_id: Mapped[PyUUID | None] = mapped_column(
        ForeignKey("data_transfers.id"), nullable=True
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="inputs")
    s3_object: Mapped[S3Object] = relationship(back_populates="run_inputs")
    data_transfer: Mapped[DataTransfer | None] = relationship(back_populates="run_input")


class RunOutput(Base):
    __tablename__ = "run_outputs"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "s3_object_id"),
        UniqueConstraint("data_transfer_id"),
    )

    run_id: Mapped[PyUUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)
    data_transfer_id: Mapped[PyUUID | None] = mapped_column(
        ForeignKey("data_transfers.id"), nullable=True
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="outputs")
    s3_object: Mapped[S3Object] = relationship(back_populates="run_outputs")
    data_transfer: Mapped[DataTransfer | None] = relationship(back_populates="run_output")


class RunMetric(Base):
    __tablename__ = "run_metrics"

    run_id: Mapped[PyUUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    max_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    final_design_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    run: Mapped[WorkflowRun] = relationship(back_populates="metrics")


DataTransferDirection = Literal["input", "output"]
# Driven by app/services/globus_transfer.py for provider="globus" rows.
DataTransferStatus = Literal["pending", "in_progress", "completed", "failed"]


class DataTransfer(Base):
    """
    Records a single data transfer (upload/download) performed as part of a
    workflow run. ``provider`` identifies which backend performed the
    transfer (e.g. "s3", "globus"), so new providers can be supported without
    schema changes.
    """

    __tablename__ = "data_transfers"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    workflow_run_id: Mapped[PyUUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    direction: Mapped[DataTransferDirection] = mapped_column(String(length=10), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    destination_location: Mapped[str] = mapped_column(Text, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Before submission succeeds, holds Globus's submission id (an idempotency
    # key generated up front so a crash-and-retry doesn't double-submit);
    # overwritten with the real Globus task id once submission succeeds, which
    # is what's used to poll status thereafter.
    transfer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DataTransferStatus] = mapped_column(
        String(length=20), nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="data_transfers")
    run_input: Mapped[RunInput | None] = relationship(back_populates="data_transfer")
    run_output: Mapped[RunOutput | None] = relationship(back_populates="data_transfer")

    def reset_to_pending(self, session: Session, commit: bool = True):
        """
        Reset a transfer to pending so it gets attempted again
        """
        now = datetime.now(UTC)
        self.status = DataTransferStatus.PENDING
        self.transfer_id = None
        self.error_message = None
        self.updated_at = now
        session.add(self)
        if commit:
            session.commit()
            
