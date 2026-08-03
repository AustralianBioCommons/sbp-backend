"""Core database models for workflows and run metadata."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
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

from .. import Base

_InetType = Text().with_variant(INET(), "postgresql")


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    auth0_user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    credit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    credit_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credit_updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
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

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    workflow_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflows.id"))
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("app_users.id"), nullable=False)
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

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)
    data_transfer_id: Mapped[UUID | None] = mapped_column(
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

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)
    data_transfer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("data_transfers.id"), nullable=True
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="outputs")
    s3_object: Mapped[S3Object] = relationship(back_populates="run_outputs")
    data_transfer: Mapped[DataTransfer | None] = relationship(back_populates="run_output")


class RunMetric(Base):
    __tablename__ = "run_metrics"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    max_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    final_design_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    run: Mapped[WorkflowRun] = relationship(back_populates="metrics")


DataTransferDirection = Literal["input", "output"]
# Placeholder values - nothing transitions these yet, so revisit this set once
# the actual transfer/execution mechanism (provider, retries, etc.) is settled.
DataTransferStatus = Literal["pending", "in_progress", "completed", "failed"]


class DataTransfer(Base):
    """
    Records a single data transfer (upload/download) performed as part of a
    workflow run. ``provider`` identifies which backend performed the
    transfer (e.g. "s3", "globus"), so new providers can be supported without
    schema changes; provider-specific details that don't fit the common
    columns can be stored in ``provider_metadata``.
    """

    __tablename__ = "data_transfers"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    workflow_run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    direction: Mapped[DataTransferDirection] = mapped_column(String(length=10), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    destination_location: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DataTransferStatus] = mapped_column(
        String(length=20), nullable=False, default="pending"
    )
    provider_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
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
