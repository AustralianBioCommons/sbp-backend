"""Pydantic models for the system health / status endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Component identifiers (kept stable: they are consumed by the portal banner and
# the admin dashboard, and used as CloudWatch metric dimensions).
COMPONENT_SEQERA_API = "seqera_api"
COMPONENT_COMPUTE_ENV = "seqera_compute_env"
COMPONENT_TOWER_AGENT = "seqera_tower_agent"

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


class ProbeResult(BaseModel):
    """Outcome of probing a single component."""

    name: str
    status: HealthStatus
    latency_ms: int | None = None
    message: str | None = None
    detail: dict[str, Any] | None = None


class SystemStatus(BaseModel):
    """Aggregated status across all probed components."""

    overall_status: HealthStatus
    checked_at: datetime
    components: list[ProbeResult]


class ComponentStatus(BaseModel):
    """Coarse, user-safe status for a single monitored component."""

    name: str
    status: HealthStatus
    latencyMs: int | None = Field(
        default=None, description="Probe round-trip latency in milliseconds"
    )
    message: str | None = Field(
        default=None, description="Short human-readable reason when not healthy"
    )


class SystemStatusResponse(BaseModel):
    """Coarse system status. Drives the portal banner (public/authenticated)."""

    overallStatus: HealthStatus
    checkedAt: datetime
    components: list[ComponentStatus]


class ComponentsHealthResponse(BaseModel):
    """Coarse, user-facing health summary for the job details page.

    Deliberately collapses all monitored components into a single signal: the
    portal does not surface *which* component is affected, only whether *some*
    dependency is degraded, so it can warn that job status / logs may be stale
    while a component is offline. Drives GET /api/health/components.
    """

    overallStatus: HealthStatus
    checkedAt: datetime
    message: str | None = Field(
        default=None,
        description=("User-facing notice shown when not healthy; null when everything is healthy"),
    )


class AgentHealthResponse(BaseModel):
    """Tower Agent health result for automated monitoring (M2M-only endpoint).

    Deliberately narrow: no raw Seqera response, credentials, or internal URLs.
    """

    status: HealthStatus
    checkedAt: datetime
    message: str | None = Field(
        default=None, description="Short human-readable reason when not healthy"
    )


class ComponentStatusDetail(ComponentStatus):
    """Verbose, admin-only status for a single component.

    Adds the raw probe detail (latency, last-error body, full Seqera compute-env
    JSON) that admins need to drill down without opening CloudWatch.
    """

    detail: dict[str, Any] | None = Field(
        default=None,
        description="Raw probe detail: error body, or full Seqera compute-env JSON",
    )


class SystemStatusAdminResponse(BaseModel):
    """Verbose system status returned by GET /admin/api/system-status."""

    overallStatus: HealthStatus
    checkedAt: datetime
    components: list[ComponentStatusDetail]
    cloudwatchLogGroupUrl: str | None = Field(
        default=None,
        description="One-click link to the backend CloudWatch log group, if configured",
    )


class GadiQueueStatusResponse(BaseModel):
    """SBP's current Gadi submission-queue occupancy, returned by
    GET /admin/api/gadi-queue-status.

    Reflects workflows this app has submitted to Gadi (Seqera SUBMITTED/RUNNING
    runs) against its configured concurrency cap - not Gadi's overall PBS-wide
    queue, which this backend has no direct visibility into.
    """

    activeWorkflows: int = Field(
        description="Workflows this app currently has SUBMITTED or RUNNING on Gadi"
    )
    maxConcurrentWorkflows: int = Field(
        description="Configured cap on concurrent workflow submissions"
    )
    availableCapacity: int = Field(description="Remaining submission slots before the cap is hit")
    checkedAt: datetime


class PbsJobEntry(BaseModel):
    """One PBS job owned by sbp_service, as reported by `qstat -u sbp_service -f`."""

    jobId: str
    jobName: str | None = None
    state: str = Field(description="Raw PBS single-letter job_state code (Q, R, H, ...)")
    stateLabel: str = Field(description="Human-readable label for `state` (Queued, Running, ...)")
    queue: str | None = None
    account: str | None = None
    submittedAt: datetime | None = None
    startedAt: datetime | None = Field(
        default=None, description="Null until the job actually starts running"
    )


class QueueTotalEntry(BaseModel):
    """Gadi-wide totals for one queue, alongside sbp_service's own share of it.

    Secondary/contextual data - e.g. "10 of our jobs running out of 100 total
    in `normal`". Best-effort: an empty list here just means that context
    wasn't available, independent of whether `jobs` above loaded fine.
    """

    name: str
    mineQueued: int
    totalQueued: int
    mineRunning: int
    totalRunning: int
    mineHeld: int
    totalHeld: int


class GadiPbsJobsResponse(BaseModel):
    """sbp_service's own Gadi PBS jobs, returned by GET /admin/api/gadi-pbs-jobs.

    This backend has no direct connection to Gadi - a script running on Gadi
    under the service account periodically pushes qstat output to S3, and this
    endpoint just reads that object back. `generatedAt` is when the Gadi-side
    script last ran, not when this endpoint was called, so a stale value here
    means that push script has stopped running, not that this request is slow.

    `jobs` (primary) is scoped to sbp_service's own jobs only, via job-listing
    mode (qstat -u) - a per-queue count (qstat -Q) has no per-user filter and
    would include every other Gadi user's jobs too. `queueTotals` (secondary)
    uses that per-queue mode anyway, purely for "ours vs. everyone's" context
    alongside the job list.
    """

    generatedAt: datetime
    jobs: list[PbsJobEntry]
    queueTotals: list[QueueTotalEntry] = Field(default_factory=list)


class ComponentIncident(BaseModel):
    """A single downtime period for one component (never ``healthy``)."""

    status: HealthStatus
    startedAt: datetime
    endedAt: datetime | None = Field(
        default=None, description="Null while the incident is still ongoing"
    )
    message: str | None = None


class ComponentDowntimeSummary(BaseModel):
    """Downtime aggregates for a component over the requested time window."""

    incidentCount: int
    downtimeSeconds: float
    uptimePercent: float = Field(
        description="Share of the window the component was healthy, as a percentage"
    )


class ComponentDowntime(BaseModel):
    """Downtime incidents and summary for a single component."""

    name: str
    summary: ComponentDowntimeSummary
    incidents: list[ComponentIncident]


class SystemStatusDowntimeResponse(BaseModel):
    """Verbose per-component downtime returned by GET /admin/api/system-status/history."""

    windowHours: int
    since: datetime
    until: datetime
    components: list[ComponentDowntime]
