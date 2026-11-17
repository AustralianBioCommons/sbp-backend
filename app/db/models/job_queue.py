from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID as PyUUID
from uuid import uuid7

from sqlalchemy import (
    JSON,
    UUID,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from ...schemas.backend import RepositoryTransferMode
from .. import Base

if TYPE_CHECKING:
    from .core import Workflow, WorkflowRun

JobStatus = Literal["staging", "pending", "launching", "submitted", "failed", "cancelled"]


class QueuedJob(Base):
    __tablename__ = "queued_jobs"
    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    workflow_run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    workflow_id: Mapped[UUID] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    launch_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    repository_transfer: Mapped[RepositoryTransferMode] = mapped_column(
        String(20), nullable=False, default=RepositoryTransferMode.NATIVE
    )
    repository_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[JobStatus] = mapped_column(String(length=20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
        nullable=False,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow: Mapped[Workflow] = relationship()
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="queued_jobs")

    @classmethod
    def create_for_launch(
        cls,
        session: Session,
        *,
        workflow_run: WorkflowRun,
        launch_payload: dict,
        repository_mode: RepositoryTransferMode,
        repository_location: str | None,
        repository_ready: bool,
        commit: bool = False,
    ) -> Self:
        """Queue a launch in staging until the readiness check clears its dependencies."""
        job = cls(
            workflow=workflow_run.workflow,
            workflow_run=workflow_run,
            launch_payload=launch_payload,
            status="staging",
            next_attempt_at=datetime.now(UTC),
        )
        job.record_repository_preparation(
            session, repository_mode, repository_location, repository_ready
        )
        session.add(job)
        if commit:
            session.commit()
        else:
            session.flush()
        return job

    def record_repository_preparation(
        self, session: Session, mode: RepositoryTransferMode, location: str | None, ready: bool
    ) -> None:
        if mode == RepositoryTransferMode.NATIVE:
            self.repository_transfer = mode
            self.repository_location = None
            self.repository_ready = True
            return

        from .backends import WorkflowRepoStatus

        if not location:
            raise ValueError("Globus repository preparation requires a checkout location")
        if not ready:
            run = self.workflow_run
            backend_id = run.backend_id or (run.backend.id if run.backend else None)
            workflow_id = run.workflow_id or (run.workflow.id if run.workflow else None)
            if backend_id is None or workflow_id is None:
                raise ValueError("Repository preparation requires a backend and workflow")
            repo = WorkflowRepoStatus.lock_for_workflow(session, backend_id, workflow_id)
            if repo is None or repo.location != location:
                raise ValueError("Repository checkout changed during preparation; retry the launch")
            ready = repo.status == "completed"
        self.repository_transfer = mode
        self.repository_location = location
        self.repository_ready = ready

    def refresh_repository_readiness(self, session: Session) -> bool:
        from .backends import WorkflowRepoStatus

        if self.repository_transfer == RepositoryTransferMode.NATIVE or self.repository_ready:
            return True
        run = self.workflow_run
        if run.backend_id is None or run.workflow_id is None or not self.repository_location:
            return False
        repo = WorkflowRepoStatus.lock_for_workflow(session, run.backend_id, run.workflow_id)
        # Completion may have been recorded while this transaction waited for the lock.
        session.refresh(self, ["repository_ready"])
        if self.repository_ready:
            return True
        if repo is None or repo.location != self.repository_location:
            return False
        if repo.status == "failed":
            self.status = "failed"
            self.error = f"Workflow repo staging failed: {repo.error_message}"
        elif repo.status == "completed":
            self.repository_ready = True
        return self.repository_ready

    @classmethod
    def get_repository_launch_payload(
        cls, session: Session, backend_id: PyUUID, workflow_id: PyUUID, location: str
    ) -> dict | None:
        from .core import WorkflowRun

        return session.scalar(
            select(cls.launch_payload)
            .join(WorkflowRun, cls.workflow_run_id == WorkflowRun.id)
            .where(
                WorkflowRun.backend_id == backend_id,
                WorkflowRun.workflow_id == workflow_id,
                cls.repository_transfer == RepositoryTransferMode.GLOBUS,
                cls.repository_location == location,
            )
            .order_by(cls.queued_at.asc())
            .limit(1)
        )

    @classmethod
    def record_repository_completion(
        cls, session: Session, backend_id: PyUUID, workflow_id: PyUUID, location: str
    ) -> None:
        from .core import WorkflowRun

        jobs = session.scalars(
            select(cls)
            .join(WorkflowRun, cls.workflow_run_id == WorkflowRun.id)
            .where(
                WorkflowRun.backend_id == backend_id,
                WorkflowRun.workflow_id == workflow_id,
                cls.repository_transfer == RepositoryTransferMode.GLOBUS,
                cls.repository_location == location,
                cls.repository_ready.is_(False),
            )
        )
        for job in jobs:
            job.repository_ready = True

    def cancel_pending_job(self, session: Session, commit: bool = False) -> None:
        self.status = "cancelled"
        self.next_attempt_at = None
        session.add(self)
        if commit:
            session.commit()

    def reserve_for_launch(
        self, session: Session, *, next_attempt_at: datetime, commit: bool = False
    ) -> None:
        self.status = "launching"
        self.next_attempt_at = next_attempt_at
        session.add(self)
        if commit:
            session.commit()

    def release_launch_reservation(
        self, session: Session, *, next_attempt_at: datetime, commit: bool = False
    ) -> None:
        self.status = "pending"
        self.next_attempt_at = next_attempt_at
        session.add(self)
        if commit:
            session.commit()
