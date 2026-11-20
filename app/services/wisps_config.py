"""WISPS (Interaction Screening / Bulk Prediction) workflow configuration."""

from __future__ import annotations

from typing import Any, Literal

from ..schemas.workflows.shared import WorkflowUserDetails
from .cluster_utils import COLABFOLD_PROCESS_OPTIONS, append_process_config
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
    """Read WISPS config and append accounting and ColabFold process overrides."""
    base = fetch_workflow_config(config_file_path)

    return append_process_config(
        base,
        user_details=user_details,
        gadi_project=gadi_project,
        process_options=COLABFOLD_PROCESS_OPTIONS,
    )
