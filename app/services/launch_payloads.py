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

    NXF_OFFLINE=true avoids network calls Gadi compute nodes can't make.
    NXF_ASSETS points at the bare repo's parent dir (see build_repo_gadi_path)
    so Nextflow's own `file:` pipeline checkout lands next to it instead of
    ~/.nextflow/assets.
    """
    lines = ["export NXF_OFFLINE=true"]
    if repo_gadi_path:
        bare_repo_path = PurePosixPath(repo_gadi_path)
        nxf_assets_path = f"{bare_repo_path.parent}/"
        lines.append(f"export NXF_ASSETS={nxf_assets_path}")
        # S3/Globus staging drops unix mode bits, so the pre-staged working
        # checkout's bin/ scripts land non-executable - restore them here,
        # the only place this backend runs a command directly on Gadi.
        working_checkout_bin = bare_repo_path.parent / "local" / bare_repo_path.stem / "bin"
        lines.append(
            f'[ -d "{working_checkout_bin}" ] && '
            f'find "{working_checkout_bin}" -type f -exec chmod +x {{}} +'
        )
    lines.extend(f"module load {module}" for module in module_loads or [])

    header = "\n".join(lines) + "\n"
    body = fetch_workflow_config(prerun_script_path) if prerun_script_path else ""
    return header + body
