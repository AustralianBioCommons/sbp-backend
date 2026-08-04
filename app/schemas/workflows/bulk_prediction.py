"""Schemas for the bulk-prediction workflow.

Bulk-prediction runs on the same WISPS pipeline as interaction-screening and
has no fields or response shapes of its own, so it reuses ``WispsFormData``
from ``interaction_screening.py`` (the generic ``DatasetUploadResponse``/
``S3DatasetUploadResponse`` in ``shared.py`` cover its upload responses).
"""

from __future__ import annotations

from .interaction_screening import WispsFormData

__all__ = ["WispsFormData"]
