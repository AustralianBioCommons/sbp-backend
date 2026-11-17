"""Proteinfold workflow configuration and executor settings."""

from __future__ import annotations

from typing import Any


def get_proteinfold_default_params(
    out_dir: str, samplesheet_url: str, mode: str = "alphafold2"
) -> dict[str, Any]:
    """Get default parameters for proteinfold workflow."""
    return {"input": samplesheet_url, "outdir": out_dir, "mode": mode}


def get_proteinfold_config_profiles() -> list[str]:
    """Get config profiles for proteinfold workflow."""
    return ["singularity"]
