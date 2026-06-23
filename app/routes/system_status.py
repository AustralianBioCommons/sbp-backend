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

from fastapi import APIRouter, Depends, Query

from ..db.admin import require_admin_access
from ..schemas.health import SystemStatusAdminResponse
from ..services import health

router = APIRouter(
    tags=["system-status"],
    dependencies=[Depends(require_admin_access)],
)


@router.get("/system-status", response_model=SystemStatusAdminResponse)
async def get_admin_system_status(
    refresh: bool = Query(
        default=False,
        description="Bypass the short-lived cache and re-run the probes now",
    ),
) -> SystemStatusAdminResponse:
    """Return verbose, admin-only runtime health of the submission components."""
    status_obj = await health.get_system_status(force_refresh=refresh)
    return SystemStatusAdminResponse.model_validate(health.to_admin_dict(status_obj))
