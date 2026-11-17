"""Proteinfold workflow executor for Seqera Platform."""

from __future__ import annotations

import shlex
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import ComputeBackend, QueuedJob, WorkflowRun
from ..schemas.backend import BackendLaunchConfig, ComputeBackendName, RepositoryTransferMode
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .compute_backends import validate_backend_launch_config
from .data_movement import DataMovement
from .launch_payloads import DEFAULT_MODULE_LOADS, get_executor_script, send_queued_launch
from .proteinfold_config import (
    get_proteinfold_config_profiles,
    get_proteinfold_default_params,
)
from .seqera import WorkflowLaunchResult, params_to_yaml_text
from .seqera_errors import WorkflowLaunchError
from .workflow_config_fetcher import render_workflow_config
from .workflow_repo_staging import RepositoryPreparation

# Params forwarded from the frontend's Tool Settings (step 2)
_TOOL_PARAM_KEYS = frozenset(
    {
        "random_seed",
        "alphafold2_full_dbs",
        "colabfold_num_recycles",
        "colabfold_use_templates",
        "boltz_use_potentials",
    }
)


def _tool_params(form_data: WorkflowFormData) -> dict[str, Any]:
    extra = form_data.extra_fields
    return {key: extra[key] for key in _TOOL_PARAM_KEYS if key in extra and extra[key] is not None}


def _build_params_text(
    out_dir: str,
    samplesheet_url: str,
    mode: str,
    form_data: WorkflowFormData | None,
    custom_params: str | None,
) -> str:
    """Build the YAML params string for the Seqera launch payload."""
    params = get_proteinfold_default_params(out_dir, samplesheet_url, mode)
    if form_data:
        params.update(_tool_params(form_data))
    params_text = params_to_yaml_text(params)
    if custom_params and custom_params.strip():
        params_text = f"{params_text}\n{custom_params.rstrip()}"
    return params_text


async def prepare_proteinfold_workflow(
    form: WorkflowLaunchForm,
    *,
    settings: Settings,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    backend: ComputeBackend,
    launch_config: BackendLaunchConfig,
    repository: RepositoryPreparation,
    revision: str | None = None,
    output_id: str | None = None,
    mode: str = "alphafold2",
    form_data: WorkflowFormData | None = None,
    user_details: WorkflowUserDetails,
    staged_input_location: str,
    commit: bool = False,
) -> QueuedJob:
    """Build and queue a proteinfold launch payload."""
    validate_backend_launch_config(workflow_run, backend, launch_config)
    movement = DataMovement(launch_config.data_transfer, settings, launch_config.globus)
    workspace_id = launch_config.workspace_id
    compute_env_id = launch_config.compute_env_id

    if not output_id or not output_id.strip():
        raise WorkflowLaunchError("Missing output identifier for workflow launch")
    out_dir = movement.output_location(output_id.strip(), "single-prediction")

    if not form.runName or not form.runName.strip():
        raise WorkflowLaunchError("Missing run name for workflow launch")

    sheet_url = staged_input_location
    params_text = _build_params_text(
        out_dir,
        sheet_url,
        mode,
        form_data,
        form.paramsText,
    )

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": form.runName,
        "pipeline": pipeline,
        "workDir": workflow_run.work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_proteinfold_config_profiles(),
        "configText": render_workflow_config(
            launch_config.config_path,
            user_details=user_details,
            gadi_project=launch_config.project_id,
            pbs_enabled=backend.name == ComputeBackendName.GADI,
        ),
        "resume": False,
    }

    workflow = workflow_run.workflow
    assert workflow is not None, "Queued job's workflow run has no associated workflow"
    prerun_script = get_executor_script(
        prerun_script_path=workflow.prerun_script_path,
        repo_checkout_path=repository.location,
        repo_url=workflow.repo_url,
        module_loads=DEFAULT_MODULE_LOADS if backend.name == ComputeBackendName.GADI else [],
        offline=repository.mode == RepositoryTransferMode.GLOBUS,
    )
    if launch_config.ref_database_dir:
        prerun_script += f"\nexport PF_DB_BASE_DIR={shlex.quote(launch_config.ref_database_dir)}\n"
    launch_payload["preRunScript"] = prerun_script

    return QueuedJob.create_for_launch(
        db_session,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        repository_mode=repository.mode,
        repository_location=repository.location,
        repository_ready=repository.ready,
        commit=commit,
    )


async def launch_proteinfold_workflow(
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a proteinfold workflow on the Seqera Platform."""
    settings = settings or get_settings()
    return await send_queued_launch(
        queued_job=queued_job, settings=settings, workflow_label="Proteinfold", dry_run=dry_run
    )
