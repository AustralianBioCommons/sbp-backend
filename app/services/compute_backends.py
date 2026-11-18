import random
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from ..config import GlobusSettings
from ..db.models import BackendWorkflowSettings, ComputeBackend, Workflow, WorkflowRun
from ..db.models.backends import BackendSelectionSettings
from ..schemas.backend import BackendLaunchConfig
from .globus_errors import GlobusConfigurationError
from .seqera_errors import WorkflowLaunchError
from .workflow_config_fetcher import validate_config_path


def get_next_backend_for_launch(
    session: Session,
    selection_settings: BackendSelectionSettings | None,
    workflow: Workflow,
) -> ComputeBackend:
    """
    Select the next compute backend to launch a job on, based on
    the current selection settings.
    """
    if selection_settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend selection settings are not configured.",
        )
    try:
        return select_next_backend(session, selection_settings, workflow)
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No compute backend could be selected.",
        ) from exc


def select_next_backend(
    session: Session, selection_settings: BackendSelectionSettings, workflow: Workflow
) -> ComputeBackend:
    if selection_settings.selection_method == "random":
        return get_backend_random(session, workflow)
    elif selection_settings.selection_method == "cycle":
        return get_backend_cycle(session, selection_settings, workflow)
    else:
        raise ValueError(f"Unknown selection method: {selection_settings.selection_method}")


def get_enabled_backends(session: Session, workflow: Workflow) -> Sequence[ComputeBackend]:
    """
    Get enabled backends that have not disabled this workflow.
    Missing workflow overrides remain eligible; order by name for stable cycling.
    """
    disabled_for_workflow = (
        select(BackendWorkflowSettings.id)
        .where(
            BackendWorkflowSettings.compute_backend_id == ComputeBackend.id,
            BackendWorkflowSettings.workflow_id == workflow.id,
            BackendWorkflowSettings.enabled.is_(False),
        )
        .exists()
    )
    return session.scalars(
        select(ComputeBackend)
        .where(ComputeBackend.enabled, ~disabled_for_workflow)
        .order_by(ComputeBackend.name)
    ).all()


def get_backend_random(session: Session, workflow: Workflow) -> ComputeBackend:
    enabled_backends = get_enabled_backends(session, workflow)
    return random.choice(enabled_backends)


def get_backend_cycle(
    session: Session, selection_settings: BackendSelectionSettings, workflow: Workflow
) -> ComputeBackend:
    backends = get_enabled_backends(session, workflow)
    if not backends:
        raise ValueError("No enabled compute backends")

    last_index = next(
        (
            index
            for index, backend in enumerate(backends)
            if backend.id == selection_settings.last_selected_id
        ),
        -1,
    )
    backend = backends[(last_index + 1) % len(backends)]
    selection_settings.record_selection(backend)
    session.add(selection_settings)
    return backend


def resolve_backend_launch_config(
    session: Session, backend: ComputeBackend, workflow: Workflow
) -> BackendLaunchConfig:
    """
    Resolve backend-specific settings before launch.

    Settings may come from:
    - BackendWorkflowSettings (preferred, if present)
    - Workflow (fallback if backend-specific setting missing/not defined)
    """
    if not backend.enabled:
        raise ValueError(f"Compute backend '{backend.name}' is disabled")
    override = BackendWorkflowSettings.get_for_workflow(session, backend.id, workflow.id)
    if override is not None and not override.enabled:
        raise ValueError(f"Workflow '{workflow.name}' is disabled on backend '{backend.name}'")
    config_path = (
        override.config_path
        if override is not None and override.config_path is not None
        else workflow.config_path
    )
    ref_database = (
        override.ref_database_dir
        if override is not None and override.ref_database_dir is not None
        else workflow.ref_database
    )
    config = BackendLaunchConfig(
        backend_id=backend.id,
        workflow_id=workflow.id,
        backend_name=backend.name,
        workspace_id=backend.seqera_workspace_id,
        compute_env_id=backend.seqera_compute_env_id,
        base_work_dir=backend.base_work_dir,
        config_path=config_path if config_path is not None else "",
        ref_database_dir=ref_database,
        project_id=backend.project_id,
        repository_transfer=backend.repository_transfer,
        data_transfer=backend.data_transfer,
        globus=backend.get_settings().globus,
    )
    validate_config_path(config.config_path)
    return config


def validate_backend_launch_config(
    workflow_run: WorkflowRun, backend: ComputeBackend, config: BackendLaunchConfig
) -> None:
    # Relationship foreign keys may not be populated until the new run is flushed.
    run_backend_id = workflow_run.backend_id
    if run_backend_id is None and workflow_run.backend is not None:
        run_backend_id = workflow_run.backend.id
    workflow_id = workflow_run.workflow_id
    if workflow_id is None and workflow_run.workflow is not None:
        workflow_id = workflow_run.workflow.id
    if (
        run_backend_id != backend.id
        or config.backend_id != backend.id
        or config.workflow_id != workflow_id
    ):
        raise WorkflowLaunchError("Backend configuration does not match the workflow run")


def get_run_globus_settings(run: WorkflowRun | None) -> GlobusSettings:
    """Resolve credentials from the transfer's assigned backend."""
    if run is None or run.backend is None:
        raise GlobusConfigurationError("Globus operations require an assigned compute backend")
    try:
        return run.backend.get_globus_settings()
    except ValueError as exc:
        raise GlobusConfigurationError(
            f"Invalid Globus settings for backend '{run.backend.name}'"
        ) from exc
