"""Admin-only system status endpoint.

Reports the runtime health of the components workflow submission depends on
(Seqera API reachability + credentials, Gadi-backed compute environment). Returns
verbose detail (latencies, last-error bodies, full compute-env JSON) and is gated
behind admin access.

This router is mounted in ``main.py`` alongside the other API routers (not tied to
the optional admin dashboard), so it is always available for healthchecks and
external monitoring as long as the caller presents an admin token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.admin import require_admin_access
from ..schemas.health import (
    GadiPbsJobsResponse,
    GadiQueueStatusResponse,
    PbsJobEntry,
    QueueTotalEntry,
    SystemStatusAdminResponse,
    SystemStatusDowntimeResponse,
)
from ..services import gadi_pbs_jobs, health, seqera
from .dependencies import get_db

router = APIRouter(
    tags=["system-status"],
    dependencies=[Depends(require_admin_access)],
)

# Bounds how far back /system-status/history can be queried.
_MAX_HISTORY_HOURS = 24 * 30


@router.get("/system-status", response_model=SystemStatusAdminResponse)
async def get_admin_system_status(
    refresh: bool = Query(
        default=False,
        description="Bypass the shared database cache and re-run the probes now",
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SystemStatusAdminResponse:
    """Return verbose, admin-only runtime health of the submission components."""
    status_obj = await health.get_system_status(db, force_refresh=refresh, settings=settings)
    return SystemStatusAdminResponse.model_validate(health.to_admin_dict(status_obj, settings))


@router.get("/gadi-queue-status", response_model=GadiQueueStatusResponse)
async def get_admin_gadi_queue_status(
    settings: Settings = Depends(get_settings),
) -> GadiQueueStatusResponse:
    """Return SBP's current Gadi submission-queue occupancy (active vs. max concurrent workflows)."""
    queue = await seqera.get_queue_status(settings=settings)
    return GadiQueueStatusResponse(
        activeWorkflows=queue.active_workflows,
        maxConcurrentWorkflows=queue.max_concurrent_workflows,
        availableCapacity=queue.available_capacity,
        checkedAt=datetime.now(UTC),
    )


@router.get("/gadi-pbs-jobs", response_model=GadiPbsJobsResponse)
async def get_admin_gadi_pbs_jobs(
    settings: Settings = Depends(get_settings),
) -> GadiPbsJobsResponse:
    """Return sbp_service's own Gadi PBS jobs, as last pushed from Gadi to S3.

    Scoped to sbp_service's own jobs only (qstat -u), not cluster-wide queue
    congestion - qstat -Q's per-queue counts include every other Gadi user's
    jobs too, with no per-user filter available in that mode. Raises on a
    missing or malformed S3 object (see GadiPbsJobsError) rather than masking
    it as an empty result - that usually means the Gadi-side push script has
    stopped.
    """
    snapshot = await gadi_pbs_jobs.get_pbs_jobs(settings=settings)
    return GadiPbsJobsResponse(
        generatedAt=snapshot.generated_at,
        jobs=[
            PbsJobEntry(
                jobId=j.job_id,
                jobName=j.job_name,
                state=j.state,
                stateLabel=j.state_label,
                queue=j.queue,
                account=j.account,
                submittedAt=j.submitted_at,
                startedAt=j.started_at,
            )
            for j in snapshot.jobs
        ],
        queueTotals=[
            QueueTotalEntry(
                name=q.name,
                mineQueued=q.mine_queued,
                totalQueued=q.total_queued,
                mineRunning=q.mine_running,
                totalRunning=q.total_running,
                mineHeld=q.mine_held,
                totalHeld=q.total_held,
            )
            for q in snapshot.queue_totals
        ],
    )


@router.get("/system-status/history", response_model=SystemStatusDowntimeResponse)
async def get_admin_system_status_history(
    hours: int = Query(
        default=24,
        ge=1,
        le=_MAX_HISTORY_HOURS,
        description="How many hours of downtime history to return",
    ),
    component: str | None = Query(default=None, description="Restrict to a single component name"),
    db: Session = Depends(get_db),
) -> SystemStatusDowntimeResponse:
    """Return per-component downtime incidents, so admins can see how frequently
    and for how long a component has been degraded/unhealthy over time."""
    until = datetime.now(UTC)
    since = until - timedelta(hours=hours)
    incidents = health.get_incidents(db, since=since, until=until, component=component)
    return SystemStatusDowntimeResponse.model_validate(
        health.to_downtime_dict(incidents, since=since, until=until, hours=hours)
    )
