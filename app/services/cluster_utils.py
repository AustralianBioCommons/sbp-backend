"""Utilities for building HPC cluster option strings."""

from __future__ import annotations

import base64
import os

GADI_PROJECT: str = os.getenv("GADI_PROJECT", "yz52")


def encode_value(value: str) -> str:
    """Return the base64 encoding of a string."""
    return base64.b64encode(value.encode()).decode()
