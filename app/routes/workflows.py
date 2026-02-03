"""Workflow router composed from smaller route modules."""

from __future__ import annotations

from fastapi import APIRouter

from .workflow.jobs import router as jobs_router
from .workflow.launch import router as launch_router
from .workflow.placeholders import router as placeholders_router

router = APIRouter(tags=["workflows"])
router.include_router(launch_router)
router.include_router(jobs_router)
router.include_router(placeholders_router)
