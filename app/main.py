"""FastAPI application entry point for the SBP Portal backend."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    from .db.admin import mount_db_admin
    from .routes.admins import router as admin_router
    from .routes.fasta_upload import router as fasta_router
    from .routes.health import agent_router as agent_health_router
    from .routes.health import router as health_router
    from .routes.pdb_upload import router as pdb_router
    from .routes.s3_files import router as s3_router
    from .routes.system_status import router as system_status_router
    from .routes.users import router as users_router
    from .routes.workflow.jobs import router as workflow_jobs_router
    from .routes.workflow.results import router as results_router
    from .routes.workflows import router as workflow_router

    settings = get_settings()
    app = FastAPI(title="SBP Portal Backend", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}

    app.include_router(workflow_router, prefix="/api/workflows")
    app.include_router(workflow_jobs_router, prefix="/api/jobs")
    app.include_router(results_router, prefix="/api/results")
    app.include_router(pdb_router, prefix="/api/workflows/pdb")
    app.include_router(fasta_router, prefix="/api/workflows/fasta")
    app.include_router(users_router, prefix="/api/users")
    app.include_router(admin_router, prefix="/api/users")
    app.include_router(health_router, prefix="/api/health")
    app.include_router(agent_health_router, prefix="/api/health")
    app.include_router(s3_router)
    # Registered before mount_db_admin so the /admin/api/system-status APIRoute
    # is matched ahead of the admin's greedy Mount("/admin"). Admin-gated, so it
    # is safe to expose independently of the optional dashboard (ENABLE_DB_ADMIN)
    # and usable for healthchecks / monitoring.
    app.include_router(system_status_router, prefix="/admin/api")
    mount_db_admin(app, settings=settings)

    @app.exception_handler(Exception)
    async def handle_exception(request: Request, exc: Exception):  # type: ignore[override]
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "message": str(exc),
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.uvicorn_reload,
    )
