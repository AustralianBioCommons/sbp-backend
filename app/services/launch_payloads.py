"""Helpers for safely preparing Seqera launch payloads."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .workflow_config_fetcher import fetch_workflow_config

DEFAULT_MODULE_LOADS = ["singularity", "nextflow/25.10.4"]


def without_prerun_script(launch_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy safe to persist in the job queue."""
    persisted_payload = launch_payload.copy()
    persisted_payload.pop("preRunScript", None)
    return persisted_payload


def inject_prerun_script(
    launch_payload: dict[str, Any],
    prerun_script: str,
) -> dict[str, Any]:
    """Return a launch-time payload with preRunScript generated at send time."""
    runtime_payload = launch_payload.copy()
    runtime_payload["preRunScript"] = prerun_script
    return runtime_payload


def get_executor_script(
    *,
    prerun_script_path: str | None,
    repo_gadi_path: str | None,
    module_loads: list[str] | None = None,
) -> str:
    """Build a pre-run script from Nextflow env vars, module loads, and a script body.

    NXF_OFFLINE=true stops Nextflow reaching out to plugin/registry endpoints
    Gadi compute nodes can't route to. Nextflow still resolves the `file:`
    bare-repo pipeline (see build_repo_gadi_path) by cloning it into
    $NXF_ASSETS/local/<name> - pointing NXF_ASSETS at that repo's own
    owner-repo directory (repo_gadi_path's parent, one level up from the
    `<commit_sha>.git` bare repo) keeps that clone alongside the repo it
    came from instead of Nextflow's global default (~/.nextflow/assets).
    """
    lines = ["export NXF_OFFLINE=true"]
    if repo_gadi_path:
        nxf_assets_path = f"{PurePosixPath(repo_gadi_path).parent}/"
        lines.append(f"export NXF_ASSETS={nxf_assets_path}")
    lines.extend(f"module load {module}" for module in module_loads or [])

    header = "\n".join(lines) + "\n"
    body = fetch_workflow_config(prerun_script_path) if prerun_script_path else ""
    return header + body
