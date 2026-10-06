"""Helpers for building and sending Seqera launch payloads."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .seqera import WorkflowLaunchResult, post_seqera_launch
from .workflow_config_fetcher import fetch_workflow_config

if TYPE_CHECKING:
    from ..config import Settings
    from ..db.models import QueuedJob

logger = logging.getLogger(__name__)

DEFAULT_MODULE_LOADS = ["singularity", "nextflow/25.10.4"]


async def send_queued_launch(
    *,
    queued_job: QueuedJob,
    settings: Settings,
    workflow_label: str,
    dry_run: bool = False,
) -> WorkflowLaunchResult | None:
    """Post a queued job's already-built launch_payload to Seqera as-is."""
    launch_payload = queued_job.launch_payload
    logger.info("Launch payload paramsText", extra={"paramsText": launch_payload["paramsText"]})
    logger.info(
        "Launching %s workflow via Seqera API",
        workflow_label,
        extra={
            "workspaceId": launch_payload["workspaceId"],
            "computeEnvId": launch_payload["computeEnvId"],
            "pipeline": launch_payload["pipeline"],
            "runName": launch_payload["runName"],
        },
    )

    if dry_run:
        logger.info("Dry run - not launching %s workflow", workflow_label)
        return None
    return await post_seqera_launch(
        {"launch": launch_payload}, workflow_label=workflow_label, settings=settings
    )


def get_executor_script(
    *,
    prerun_script_path: str | None,
    repo_gadi_path: str | None,
    repo_url: str,
    module_loads: list[str] | None = None,
) -> str:
    """Build a pre-run script from Nextflow env vars, module loads, and a script body.

    NXF_OFFLINE=true avoids network calls Gadi compute nodes can't make.
    NXF_ASSETS is two levels above the checkout (repo_gadi_path is
    .../<commit_sha>/<owner>/<repo>) so Nextflow's own <owner>/<repo>
    resolution finds the pre-staged checkout instead of fetching it.
    """
    lines = ["export NXF_OFFLINE=true"]
    if repo_gadi_path:
        bare_repo_path = PurePosixPath(repo_gadi_path)
        nxf_assets_path = f"{bare_repo_path.parent.parent}/"
        lines.append(f"export NXF_ASSETS={nxf_assets_path}")
        # S3/Globus staging drops the execute bit, so the checkout's bin/
        # scripts land non-executable - restore it here, the only place
        # this backend runs a command directly on Gadi.
        working_checkout_bin = bare_repo_path / "bin"
        lines.append(
            f'[ -d "{working_checkout_bin}" ] && '
            f'find "{working_checkout_bin}" -type f -exec chmod +x {{}} +'
        )
    lines.extend(f"module load {module}" for module in module_loads or [])

    header = "\n".join(lines) + "\n"
    body = fetch_workflow_config(prerun_script_path) if prerun_script_path else ""
    return header + body
