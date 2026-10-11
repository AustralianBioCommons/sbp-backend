"""WISPS interaction screening workflow executor for Seqera Platform."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .globus_transfer import build_gadi_output_path
from .launch_payloads import DEFAULT_MODULE_LOADS, get_executor_script, send_queued_launch
from .seqera import WorkflowLaunchResult, params_to_yaml_text
from .seqera_errors import WorkflowLaunchError
from .wisps_config import (
    WISPS_WORKFLOW_MODES,
    get_wisps_config_profiles,
    get_wisps_config_text,
    get_wisps_default_params,
)


async def prepare_wisps_workflow(
    form: WorkflowLaunchForm,
    *,
    settings: Settings,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    form_data: WorkflowFormData,
    revision: str | None = None,
    output_id: str | None = None,
    user_details: WorkflowUserDetails,
    staged_input_location: str,
    commit: bool = False,
) -> QueuedJob:
    tool: str | None = form_data.tool or None

    workspace_id = settings.seqera.work_space
    compute_env_id = settings.seqera.compute_id
    work_dir = settings.seqera.work_dir

    if not output_id or not output_id.strip():
        raise WorkflowLaunchError("Missing output identifier for workflow launch")
    out_dir = build_gadi_output_path(
        output_id.strip(),
        form_data.workflow,
        globus_settings=settings.globus,
    )

    job_id = (form.runName or "").strip()
    if not job_id:
        raise WorkflowLaunchError("Missing run name for workflow launch")

    mode = WISPS_WORKFLOW_MODES.get(form_data.workflow, "g1-g2")
    sheet_url = staged_input_location
    params_text = params_to_yaml_text(
        get_wisps_default_params(
            out_dir=out_dir,
            samplesheet_url=sheet_url,
            mode=mode,
            tool=tool,
        )
    )

    config_text = get_wisps_config_text(
        config_path,
        user_details=user_details,
        gadi_project=settings.seqera.gadi_project,
    )

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": form.runName,
        "pipeline": pipeline,
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "main",
        "paramsText": params_text,
        "configProfiles": get_wisps_config_profiles(),
        "configText": config_text,
        "resume": False,
    }

    workflow = workflow_run.workflow
    assert workflow is not None, "Queued job's workflow run has no associated workflow"
    # Each samplesheet row already points at a staged multi-FASTA (rewritten at
    # launch) that the pipeline splits itself, so there's no remote prerun
    # script: just load the modules the pipeline needs.
    prerun_script = get_executor_script(
        prerun_script_path=None,
        repo_gadi_path=workflow.repo_gadi_path,
        repo_url=workflow.repo_url,
        module_loads=DEFAULT_MODULE_LOADS,
    )
    if workflow.ref_database:
        prerun_script += f"\nexport PF_DB_BASE_DIR={shlex.quote(workflow.ref_database)}\n"
    launch_payload["preRunScript"] = prerun_script

    queued_job = QueuedJob(
        workflow=workflow,
        workflow_run=workflow_run,
        launch_payload=launch_payload,
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(queued_job)
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return queued_job


async def launch_wisps_workflow(
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch an interaction screening (WISPS) workflow on the Seqera Platform."""
    settings = settings or get_settings()
    return await send_queued_launch(
        queued_job=queued_job, settings=settings, workflow_label="WISPS", dry_run=dry_run
    )
