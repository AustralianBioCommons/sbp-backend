"""ProteinDJ workflow executor for Seqera Platform."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import ComputeBackend, QueuedJob, WorkflowRun
from ..schemas.backend import BackendLaunchConfig, ComputeBackendName, RepositoryTransferMode
from ..schemas.workflows.de_novo_design import ProteinDjFormData
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .compute_backends import validate_backend_launch_config
from .data_movement import DataMovement
from .launch_payloads import DEFAULT_MODULE_LOADS, get_executor_script, send_queued_launch
from .proteindj_config import (
    get_proteindj_config_profiles,
    get_proteindj_default_params,
    get_proteindj_design_mode,
)
from .results_utils import s3_uri_to_key
from .seqera import WorkflowLaunchResult, params_to_yaml_text
from .seqera_errors import WorkflowLaunchError
from .workflow_config_fetcher import render_workflow_config
from .workflow_repo_staging import RepositoryPreparation


def _design_length(fields: ProteinDjFormData) -> str:
    # The frontend's Input Configuration step (shared with bindcraft) sends
    # min_length/max_length as separate fields; ProteinDJ expects them as a
    # single "min-max" range.
    return f"{fields.min_length}-{fields.max_length}"


def _parse_proteindj_form_data(form_data: WorkflowFormData) -> ProteinDjFormData:
    try:
        return ProteinDjFormData.model_validate(form_data.model_dump())
    except ValidationError as exc:
        missing = "formData"
        for error in exc.errors():
            loc = error.get("loc")
            if loc:
                *_, field_name = loc  # last element of the location path is the field name
                missing = str(field_name)
                break
        raise WorkflowLaunchError(
            f"'{missing}' is required in formData for ProteinDJ workflow launch"
        ) from exc


async def prepare_proteindj_workflow(  # pylint: disable=too-many-locals
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
    form_data: WorkflowFormData,
    user_details: WorkflowUserDetails,
    commit: bool = False,
) -> QueuedJob:
    """Build and queue a proteindj launch payload."""
    validate_backend_launch_config(workflow_run, backend, launch_config)
    movement = DataMovement(launch_config.data_transfer, settings, launch_config.globus)
    workspace_id = launch_config.workspace_id
    compute_env_id = launch_config.compute_env_id

    run_name = (form.runName or "").strip()
    if not run_name:
        raise WorkflowLaunchError("Missing run name for workflow launch")
    # Always use a unique backend-generated ID for outputs to avoid S3 prefix collisions.
    output_key = (output_id or "").strip()
    if not output_key:
        raise WorkflowLaunchError("Missing output identifier for workflow launch")
    out_dir = movement.output_location(output_key, "de-novo-design")

    proteindj_fields = _parse_proteindj_form_data(form_data)

    pdb_key = s3_uri_to_key(proteindj_fields.starting_pdb)
    if not pdb_key:
        raise WorkflowLaunchError("Invalid S3 URI for starting_pdb")
    staged_pdb_location = movement.prepare_input(
        db_session,
        workflow_run.id,
        "de-novo-design",
        proteindj_fields.starting_pdb,
    )

    default_params = get_proteindj_default_params(
        out_dir,
        input_pdb=staged_pdb_location,
        hotspot_residues=proteindj_fields.target_hotspot_residues,
        num_designs=proteindj_fields.number_of_final_designs,
        design_length=_design_length(proteindj_fields),
        design_mode=get_proteindj_design_mode(form_data.tool),
    )

    # Serialize to YAML
    params_text = params_to_yaml_text(default_params)

    # Add custom paramsText from frontend if provided
    if form.paramsText and form.paramsText.strip():
        params_text = f"{params_text}\n{form.paramsText.rstrip()}"

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": run_name,
        "pipeline": pipeline,
        "workDir": workflow_run.work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_proteindj_config_profiles(),
        "configText": render_workflow_config(
            launch_config.config_path,
            user_details=user_details,
            pbs_enabled=backend.name == ComputeBackendName.GADI,
            gadi_project=launch_config.project_id,
        ),
        "resume": False,
    }

    workflow = workflow_run.workflow
    assert workflow is not None, "Queued job's workflow run has no associated workflow"
    launch_payload["preRunScript"] = get_executor_script(
        prerun_script_path=workflow.prerun_script_path,
        repo_checkout_path=repository.location,
        repo_url=workflow.repo_url,
        module_loads=DEFAULT_MODULE_LOADS if backend.name == ComputeBackendName.GADI else [],
        offline=repository.mode == RepositoryTransferMode.GLOBUS,
    )

    return QueuedJob.create_for_launch(
        db_session,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        repository_mode=repository.mode,
        repository_location=repository.location,
        repository_ready=repository.ready,
        commit=commit,
    )


async def launch_proteindj_workflow(
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a proteindj workflow on the Seqera Platform."""
    settings = settings or get_settings()
    return await send_queued_launch(
        queued_job=queued_job, settings=settings, workflow_label="ProteinDJ", dry_run=dry_run
    )
