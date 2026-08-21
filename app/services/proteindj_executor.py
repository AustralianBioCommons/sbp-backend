"""ProteinDJ workflow executor for Seqera Platform (modeled after bindflow)."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.models import QueuedJob, WorkflowRun
from ..db.models.core import DataTransfer, RunInput, S3Object
from ..schemas.workflows.de_novo_design import ProteinDjFormData
from ..schemas.workflows.shared import WorkflowFormData, WorkflowLaunchForm, WorkflowUserDetails
from .globus_transfer import build_gadi_input_path, build_gadi_output_path
from .launch_payloads import (
    DEFAULT_MODULE_LOADS,
    get_executor_script,
    inject_prerun_script,
    without_prerun_script,
)
from .proteindj_config import (
    get_proteindj_config_profiles,
    get_proteindj_config_text,
    get_proteindj_default_params,
)
from .results_utils import s3_uri_to_key
from .seqera import (
    WorkflowLaunchResult,
    params_to_yaml_text,
    post_seqera_launch,
)
from .seqera_errors import WorkflowLaunchError

logger = logging.getLogger(__name__)


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
    config_path: str,
    revision: str | None = None,
    output_id: str | None = None,
    form_data: WorkflowFormData,
    user_details: WorkflowUserDetails,
    commit: bool = False,
) -> QueuedJob:
    """Build and queue a proteindj launch payload."""
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

    proteindj_fields = _parse_proteindj_form_data(form_data)

    pdb_key = s3_uri_to_key(proteindj_fields.starting_pdb)
    if not pdb_key:
        raise WorkflowLaunchError("Invalid S3 URI for starting_pdb")
    if db_session.get(S3Object, pdb_key) is None:
        db_session.add(S3Object(object_key=pdb_key, uri=proteindj_fields.starting_pdb))
    staged_pdb_location = build_gadi_input_path(
        workflow_run.id,
        "de-novo-design",
        os.path.basename(pdb_key),
        globus_settings=settings.globus,
    )
    pdb_transfer = DataTransfer(
        workflow_run_id=workflow_run.id,
        direction="input",
        provider="globus",
        source_location=proteindj_fields.starting_pdb,
        destination_location=staged_pdb_location,
        recursive=False,
    )
    db_session.add(pdb_transfer)
    db_session.add(
        RunInput(run_id=workflow_run.id, s3_object_id=pdb_key, data_transfer=pdb_transfer)
    )

    default_params = get_proteindj_default_params(
        out_dir,
        input_pdb=staged_pdb_location,
        hotspot_residues=proteindj_fields.target_hotspot_residues,
        num_designs=proteindj_fields.number_of_final_designs,
        design_length=_design_length(proteindj_fields),
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
        "workDir": work_dir,
        "workspaceId": workspace_id,
        "revision": revision or "dev",
        "paramsText": params_text,
        "configProfiles": get_proteindj_config_profiles(),
        "configText": get_proteindj_config_text(
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


async def launch_proteindj_workflow(  # pylint: disable=too-many-locals
    *,
    queued_job: QueuedJob,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Launch a proteindj workflow on the Seqera Platform."""
    settings = settings or get_settings()
    launch_payload = queued_job.launch_payload

    # Log the complete params being sent
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})

    logger.info(
        "Launching proteindj workflow via Seqera API",
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
        logger.info("Dry run - not launching proteindj workflow")
        return None
    return await post_seqera_launch(
        {"launch": runtime_payload}, workflow_label="ProteinDJ", settings=settings
    )
