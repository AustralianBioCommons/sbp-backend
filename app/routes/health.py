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

Also exposes a machine-only ``GET /agent`` on its own ``agent_router``, since
it must skip ``get_current_user_id`` for M2M clients.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..schemas.health import (
    COMPONENT_TOWER_AGENT,
    AgentHealthResponse,
    ComponentsHealthResponse,
)
from ..services import health
from .dependencies import (
    get_current_user_id,
    get_db,
    require_agent_health_permission,
    require_workflow_execution_role,
)

router = APIRouter(
    tags=["health"],
    dependencies=[Depends(get_current_user_id), Depends(require_workflow_execution_role)],
)

agent_router = APIRouter(tags=["health"])


@router.get("/components", response_model=ComponentsHealthResponse)
async def get_components_health(db: Session = Depends(get_db)) -> ComponentsHealthResponse:
    """Return a coarse, user-facing health summary for SBP-bundle users.

    ``overallStatus`` is the worst status across all monitored components; when it
    is not ``healthy`` a generic ``message`` is included for display on the job
    details page.

    Always comes from the shared database cache
    """
    status_obj = await health.get_system_status(db)
    return ComponentsHealthResponse.model_validate(health.to_components_health_dict(status_obj))


@agent_router.get(
    "/agent",
    response_model=AgentHealthResponse,
    dependencies=[Depends(require_agent_health_permission)],
)
async def get_agent_health(db: Session = Depends(get_db)) -> AgentHealthResponse:
    """Return the cached Tower Agent health state for automated monitoring.

    Returns 503, not a fabricated "unhealthy", when the component is absent
    (agent monitoring disabled).
    """
    status_obj = await health.get_system_status(db)

    agent_result = next(
        (
            component
            for component in status_obj.components
            if component.name == COMPONENT_TOWER_AGENT
        ),
        None,
    )

    if agent_result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tower Agent health status is unavailable.",
        )

    return AgentHealthResponse(
        status=agent_result.status,
        checkedAt=status_obj.checked_at,
        message=agent_result.message,
    )
