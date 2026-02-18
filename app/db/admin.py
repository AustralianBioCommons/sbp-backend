"""Starlette Admin and DB debug API mounting helpers."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, FastAPI, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..routes.dependencies import get_db
from . import engine
from .models.core import (
    AppUser,
    RunInput,
    RunMetric,
    RunOutput,
    S3Object,
    Workflow,
    WorkflowRun,
)


def _is_db_admin_enabled() -> bool:
    return os.getenv("ENABLE_DB_ADMIN", "false").strip().lower() in {"1", "true", "yes"}


def mount_db_admin(app: FastAPI) -> None:
    """Mount Starlette Admin and read-only debug endpoints when enabled."""
    if not _is_db_admin_enabled():
        return

    _mount_starlette_admin(app)
    _mount_db_debug_api(app)


def _mount_starlette_admin(app: FastAPI) -> None:
    try:
        from starlette_admin.contrib.sqla import Admin, ModelView
    except ImportError as exc:  # pragma: no cover - dependency issue
        raise RuntimeError(
            "ENABLE_DB_ADMIN=true but starlette-admin is not installed."
        ) from exc

    class AppUserAdmin(ModelView):
        fields = ["id", "auth0_user_id", "name", "email"]

    class WorkflowAdmin(ModelView):
        fields = ["id", "name", "description", "repo_url", "default_revision"]

    class WorkflowRunAdmin(ModelView):
        fields = [
            "id",
            "workflow_id",
            "owner_user_id",
            "seqera_dataset_id",
            "seqera_run_id",
            "run_name",
            "binder_name",
            "work_dir",
        ]

    class RunMetricAdmin(ModelView):
        fields = ["run_id", "max_score"]

    admin = Admin(engine=engine, title=os.getenv("DB_ADMIN_TITLE", "SBP Backend Admin"))
    admin.add_view(AppUserAdmin(AppUser))
    admin.add_view(WorkflowAdmin(Workflow))
    admin.add_view(WorkflowRunAdmin(WorkflowRun))
    admin.add_view(RunMetricAdmin(RunMetric))
    admin.mount_to(app)


def _mount_db_debug_api(app: FastAPI) -> None:
    router = APIRouter(prefix="/admin/debug", tags=["admin-debug"])

    @router.get("/s3-objects")
    def list_s3_objects(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(S3Object)).scalar_one()
        rows = db.execute(
            select(S3Object).order_by(S3Object.object_key).offset(offset).limit(limit)
        ).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "object_key": row.object_key,
                    "uri": row.uri,
                    "version_id": row.version_id,
                    "size_bytes": row.size_bytes,
                }
                for row in rows
            ],
        }

    @router.get("/run-inputs")
    def list_run_inputs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(RunInput)).scalar_one()
        rows = db.execute(select(RunInput).offset(offset).limit(limit)).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "run_id": str(row.run_id),
                    "s3_object_id": row.s3_object_id,
                }
                for row in rows
            ],
        }

    @router.get("/run-outputs")
    def list_run_outputs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.execute(select(func.count()).select_from(RunOutput)).scalar_one()
        rows = db.execute(select(RunOutput).offset(offset).limit(limit)).scalars()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "run_id": str(row.run_id),
                    "s3_object_id": row.s3_object_id,
                }
                for row in rows
            ],
        }

    app.include_router(router)
