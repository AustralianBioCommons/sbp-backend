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


class PbsQueueStatusEntry(BaseModel):
    """One PBS queue's job counts, as reported by `qstat -Q -f` on Gadi."""

    name: str
    queueType: str | None = None
    enabled: bool
    started: bool
    totalJobs: int
    queued: int
    running: int
    held: int


class GadiPbsQueueStatusResponse(BaseModel):
    """Gadi-wide PBS queue status, returned by GET /admin/api/gadi-pbs-queue-status.

    This backend has no direct connection to Gadi - a script running on Gadi
    under the service account periodically pushes `qstat -Q -f` output to S3,
    and this endpoint just reads that object back. `generatedAt` is when the
    Gadi-side script last ran, not when this endpoint was called, so a stale
    value here means that push script has stopped running, not that this
    request is slow. Unlike GET /admin/api/gadi-queue-status (Seqera-tracked,
    this app's own workflows only), this reflects the whole cluster's queue
    congestion.
    """

    generatedAt: datetime
    queues: list[PbsQueueStatusEntry]


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
