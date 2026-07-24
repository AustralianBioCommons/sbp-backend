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

from ..db.admin import require_admin_access
from ..schemas.health import SystemStatusAdminResponse, SystemStatusDowntimeResponse
from ..services import health
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
) -> SystemStatusAdminResponse:
    """Return verbose, admin-only runtime health of the submission components."""
    status_obj = await health.get_system_status(db, force_refresh=refresh)
    return SystemStatusAdminResponse.model_validate(health.to_admin_dict(status_obj))


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
