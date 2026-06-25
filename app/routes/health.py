"""User-facing runtime health summary for the SBP portal.

Exposes a coarse health signal for the components workflow submission and
monitoring depend on (Seqera API reachability, the Gadi-backed compute
environment, and — when enabled — the Tower Agent). Unlike the admin endpoint
(``/admin/api/system-status``), this returns only an overall status plus a single
generic message: the portal uses it to warn SBP-bundle users on the job details
page that job status / logs may be stale while a component is offline. It does
*not* identify which component is affected.

Gated behind the same access as workflow submission (authenticated SBP-bundle
approved users), since it is only meaningful to users who can run workflows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..schemas.health import ComponentsHealthResponse
from ..services import health
from .dependencies import get_current_user_id, require_workflow_execution_role

router = APIRouter(
    tags=["health"],
    dependencies=[Depends(get_current_user_id), Depends(require_workflow_execution_role)],
)


@router.get("/components", response_model=ComponentsHealthResponse)
async def get_components_health() -> ComponentsHealthResponse:
    """Return a coarse, user-facing health summary for SBP-bundle users.

    ``overallStatus`` is the worst status across all monitored components; when it
    is not ``healthy`` a generic ``message`` is included for display on the job
    details page.

    Always served from the short-lived cache (with background refresh); there is
    no caller-triggered force-refresh here. Forcing a live probe run is reserved
    for the admin endpoint, since the Tower Agent probe can mutate Seqera state.
    """
    status_obj = await health.get_system_status()
    return ComponentsHealthResponse.model_validate(health.to_components_health_dict(status_obj))
