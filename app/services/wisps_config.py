"""WISPS (Interaction Screening / Bulk Prediction) workflow configuration."""

from __future__ import annotations

from typing import Any, Literal

from ..schemas.workflows.shared import WorkflowUserDetails
from .workflow_config_fetcher import fetch_workflow_config

WispsMode = Literal["g1-g2", "manual"]

WISPS_WORKFLOW_MODES: dict[str, WispsMode] = {
    "interaction-screening": "g1-g2",
    "bulk-prediction": "manual",
}


def get_wisps_default_params(
    out_dir: str,
    samplesheet_url: str,
    mode: WispsMode,
    tool: str | None = None,
) -> dict[str, Any]:
    """Params passed as YAML paramsText."""
    params: dict[str, Any] = {
        "outdir": out_dir,
        "input": samplesheet_url,
        "mode": mode,
    }
    if tool is not None:
        params["tools"] = tool
    return params


def get_wisps_config_profiles() -> list[str]:
    return ["singularity"]


def get_wisps_config_text(
    config_file_path: str,
    *,
    user_details: WorkflowUserDetails,
    gadi_project: str = "yz52",
) -> str:
    """Read wisps config and append a process override block with runtime values.

    Appending a process block with the
    values embedded directly overrides the file's clusterOptions.
    """
    base = fetch_workflow_config(config_file_path)

    cluster_opts = f"-P {gadi_project} -A {user_details.get_encoded_account_details()}"
    override = f'\nprocess {{\n    clusterOptions = "{cluster_opts}"\n}}\n'
    return base + override
