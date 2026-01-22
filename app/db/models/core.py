"""Core database models for workflows and run metadata."""

from sqlalchemy import BigInteger, ForeignKey, Numeric, PrimaryKeyConstraint, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .. import Base


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    auth0_user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="owner")


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_revision: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="workflow")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("seqera_run_id"),
        UniqueConstraint("work_dir"),
    )

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflows.id"))
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("app_users.id"), nullable=False)
    seqera_dataset_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    seqera_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_dir: Mapped[int] = mapped_column(BigInteger, nullable=False)

    owner: Mapped[AppUser] = relationship(back_populates="workflow_runs")
    workflow: Mapped[Workflow | None] = relationship(back_populates="runs")
    metrics: Mapped["RunMetric | None"] = relationship(back_populates="run", uselist=False)
    inputs: Mapped[list["RunInput"]] = relationship(back_populates="run")
    outputs: Mapped[list["RunOutput"]] = relationship(back_populates="run")


class S3Object(Base):
    __tablename__ = "s3_objects"
    __table_args__ = (UniqueConstraint("URI"),)

    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    uri: Mapped[str] = mapped_column("URI", Text, nullable=False)
    version_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    run_inputs: Mapped[list["RunInput"]] = relationship(back_populates="s3_object")
    run_outputs: Mapped[list["RunOutput"]] = relationship(back_populates="s3_object")


class RunInput(Base):
    __tablename__ = "run_inputs"
    __table_args__ = (PrimaryKeyConstraint("run_id", "s3_object_id"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)

    run: Mapped[WorkflowRun] = relationship(back_populates="inputs")
    s3_object: Mapped[S3Object] = relationship(back_populates="run_inputs")


class RunOutput(Base):
    __tablename__ = "run_outputs"
    __table_args__ = (PrimaryKeyConstraint("run_id", "s3_object_id"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    s3_object_id: Mapped[str] = mapped_column(ForeignKey("s3_objects.object_key"), nullable=False)

    run: Mapped[WorkflowRun] = relationship(back_populates="outputs")
    s3_object: Mapped[S3Object] = relationship(back_populates="run_outputs")


class RunMetric(Base):
    __tablename__ = "run_metrics"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    max_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)

    run: Mapped[WorkflowRun] = relationship(back_populates="metrics")
