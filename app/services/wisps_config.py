"""WISPS (Interaction Screening / Bulk Prediction) workflow configuration."""

from __future__ import annotations

from typing import Any, Literal

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
