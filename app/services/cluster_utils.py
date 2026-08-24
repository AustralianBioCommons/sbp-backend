"""Utilities for building HPC cluster option strings."""

from __future__ import annotations

from ..config import Settings


def get_gadi_project(settings: Settings) -> str:
    return settings.seqera.gadi_project
