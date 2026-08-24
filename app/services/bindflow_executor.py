"""Bindflow workflow executor for Seqera Platform."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .bindflow_config import (
    get_bindflow_config_profiles,
    get_bindflow_config_text,
    get_bindflow_default_params,
)
from .globus_transfer import build_gadi_output_path
from .launch_payloads import (
    DEFAULT_MODULE_LOADS,
    get_executor_script,
    inject_prerun_script,
    without_prerun_script,
)
from .seqera import (
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import WorkflowLaunchError

logger = logging.getLogger(__name__)

# settings_filters/settings_advanced reference default JSON files bundled in
# the bindflow repo itself. The frontend leaves these fields unset (see
# sbp-portal's de-novo-design.ts) and relies on the backend to fill in the
# local Gadi path: resolve_bindflow_asset_path below fills in the known
# default for an empty value; anything else (a genuinely custom value) is
# passed through unchanged rather than guessed at, since staging arbitrary
# user-supplied settings files isn't supported yet.
_BINDFLOW_DEFAULT_ASSET_RELATIVE_PATHS = {
    "settings_filters": "assets/bindcraft/default_filters.json",
    "settings_advanced": "assets/bindcraft/default_4stage_multimer.json",
}


def resolve_bindflow_asset_path(
    field_name: str, value: object, *, repo_assets_path: str
) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    default_relative_path = _BINDFLOW_DEFAULT_ASSET_RELATIVE_PATHS.get(field_name)
    if default_relative_path is None:
        return None
    return f"{repo_assets_path}/{default_relative_path}"


async def prepare_bindflow_workflow(  # pylint: disable=too-many-locals
    form: WorkflowLaunchForm,
    *,
    settings: Settings,
    db_session: Session,
    workflow_run: WorkflowRun,
    pipeline: str,
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    form_data: WorkflowFormData,
    user_details: WorkflowUserDetails,
    staged_input_location: str,
    repo_assets_path: str,
    commit: bool = False,
) -> QueuedJob:
    """Build and queue a bindflow launch payload."""
    workspace_id = settings.seqera.work_space
    compute_env_id = settings.seqera.compute_id
    work_dir = settings.seqera.work_dir

    run_name = (form.runName or "").strip()
    if not run_name:
        raise WorkflowLaunchError("Missing run name for workflow launch")
    # Always use a unique backend-generated ID for outputs to avoid S3 prefix collisions.
    output_key = (output_id or "").strip()
    if not output_key:
        raise WorkflowLaunchError("Missing output identifier for workflow launch")
    out_dir = build_gadi_output_path(
        output_key,
        "de-novo-design",
        globus_settings=settings.globus,
    )

    default_params = get_bindflow_default_params(out_dir, staged_input_location)
    settings_filters = resolve_bindflow_asset_path(
        "settings_filters",
        form_data.extra_fields.get("settings_filters"),
        repo_assets_path=repo_assets_path,
    )
    settings_advanced = resolve_bindflow_asset_path(
        "settings_advanced",
        form_data.extra_fields.get("settings_advanced"),
        repo_assets_path=repo_assets_path,
    )
    if settings_filters:
        default_params["settings_filters"] = settings_filters
    if settings_advanced:
        default_params["settings_advanced"] = settings_advanced

    # Serialize to YAML
    params_text = params_to_yaml_text(default_params)

    # Add custom paramsText from frontend if provided
    if form.paramsText and form.paramsText.strip():
        params_text = f"{params_text}\n{form.paramsText.rstrip()}"

    launch_payload: dict[str, Any] = {
        "computeEnvId": compute_env_id,
        "runName": run_name,
        "pipeline": pipeline,
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_bindflow_config_profiles(),
        "configText": get_bindflow_config_text(
            config_path,
            user_details=user_details,
        ),
        "resume": False,
    }

    queued_job = QueuedJob(
        workflow=workflow_run.workflow,
        workflow_run=workflow_run,
        launch_payload=without_prerun_script(launch_payload),
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    db_session.add(queued_job)
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return queued_job


async def launch_bindflow_workflow(  # pylint: disable=too-many-locals
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a bindflow workflow on the Seqera Platform."""
    settings = settings or get_settings()
    launch_payload = queued_job.launch_payload

    # Log the complete params being sent
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})

    logger.info(
        "Launching bindflow workflow via Seqera API",
        extra={
            "workspaceId": launch_payload["workspaceId"],
            "computeEnvId": launch_payload["computeEnvId"],
            "pipeline": launch_payload["pipeline"],
            "runName": launch_payload["runName"],
        },
    )

    prerun_script = get_executor_script(
        prerun_script_path=queued_job.workflow.prerun_script_path,
        module_loads=DEFAULT_MODULE_LOADS,
    )
    runtime_payload = inject_prerun_script(
        launch_payload=launch_payload, prerun_script=prerun_script
    )

    if dry_run:
        logger.info("Dry run - not launching bindflow workflow")
        return None
    return await post_seqera_launch(
        {"launch": runtime_payload}, workflow_label="Bindflow", settings=settings
    )
