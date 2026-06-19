"""Pydantic models for the system health / status endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Component identifiers (kept stable: they are consumed by the portal banner and
# the admin dashboard, and used as CloudWatch metric dimensions).
COMPONENT_SEQERA_API = "seqera_api"
COMPONENT_COMPUTE_ENV = "seqera_compute_env"

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


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
