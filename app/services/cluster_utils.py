"""Utilities for building HPC cluster option strings."""

from __future__ import annotations

import base64


def encode_ip(ip_address: str) -> str:
    """Return the base64 encoding of an IP address string."""
    return base64.b64encode(ip_address.encode()).decode()
