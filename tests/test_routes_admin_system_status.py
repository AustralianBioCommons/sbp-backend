"""Tests for the admin-only system status API endpoint."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.db.admin import require_admin_access
from app.db.models.system_status import SystemStatusIncident
from app.routes.system_status import router as system_status_router
from app.schemas.health import ProbeResult, SystemStatus
from app.services import gadi_pbs_jobs, health, seqera

DB_ADMIN_REQUIRED_ENV = {
    "AUTH_DOMAIN": "example.auth.test",
    "AUTH_CLIENT_ID": "test-client-id",
    "AUTH_AUDIENCE": "https://example.api.test",
    "DB_ADMIN_AUTH_REDIRECT_URI": "http://localhost:3000/admin/login",
    "DB_ADMIN_SESSION_SECRET": "test-session-secret",
}


def _fake_status() -> SystemStatus:
    return SystemStatus(
        overall_status="unhealthy",
        checked_at=datetime(2026, 6, 1, 3, 12, 55, tzinfo=UTC),
        components=[
            ProbeResult(name="seqera_api", status="healthy", latency_ms=240),
            ProbeResult(
                name="seqera_compute_env",
                status="unhealthy",
                latency_ms=310,
                message="Compute environment state: ERRORED (Gadi agent disconnected)",
                detail={"computeEnv": {"status": "ERRORED"}},
            ),
        ],
    )


def _build_client(monkeypatch: MonkeyPatch) -> TestClient:
    async def fake_get_system_status(
        db: object | None = None, *, force_refresh: bool = False, **_kwargs
    ) -> SystemStatus:
        _ = (db, force_refresh)
        return _fake_status()

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")
    return TestClient(app)


def test_admin_system_status_returns_verbose_payload(monkeypatch: MonkeyPatch):
    client = _build_client(monkeypatch)
    resp = client.get("/admin/api/system-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["overallStatus"] == "unhealthy"
    assert body["checkedAt"].startswith("2026-06-01T03:12:55")

    components = {c["name"]: c for c in body["components"]}
    assert components["seqera_api"]["status"] == "healthy"
    assert components["seqera_api"]["latencyMs"] == 240

    ce = components["seqera_compute_env"]
    assert ce["status"] == "unhealthy"
    assert "ERRORED" in ce["message"]
    # Verbose detail (admin-only) includes the raw compute-env JSON.
    assert ce["detail"]["computeEnv"]["status"] == "ERRORED"


def test_admin_system_status_requires_admin():
    """Without the admin dependency override, the endpoint enforces auth."""
    app = FastAPI()
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        resp = client.get("/admin/api/system-status")

    # require_admin_access raises 401 when no token/cookie is present.
    assert resp.status_code == 401


def test_admin_system_status_passes_refresh_flag(monkeypatch: MonkeyPatch):
    seen = {}

    async def fake_get_system_status(
        db: object | None = None, *, force_refresh: bool = False, **_kwargs
    ) -> SystemStatus:
        _ = db
        seen["force_refresh"] = force_refresh
        return _fake_status()

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        client.get("/admin/api/system-status?refresh=true")

    assert seen["force_refresh"] is True


def test_system_status_available_without_dashboard(client):
    """The endpoint is mounted in main.py regardless of ENABLE_DB_ADMIN.

    The default test app runs with ENABLE_DB_ADMIN=false, so the admin dashboard
    is not mounted. The endpoint must still exist (401 for missing auth), proving
    it is not tied to the dashboard startup.
    """
    resp = client.get("/admin/api/system-status")
    assert resp.status_code == 401  # present + auth-gated, not 404


def test_system_status_not_shadowed_by_admin_mount(mocker):
    """With the dashboard enabled, the route must not be shadowed by Mount("/admin").

    If shadowed, the request would be handled by the admin sub-app (HTML/redirect).
    Reaching our admin-gated JSON endpoint yields a 401 JSON response instead.
    """
    from app.main import create_app

    mocker.patch.dict(os.environ, {"ENABLE_DB_ADMIN": "true", **DB_ADMIN_REQUIRED_ENV})
    app = create_app()

    with TestClient(app) as test_client:
        resp = test_client.get("/admin/api/system-status")

    assert resp.status_code == 401  # our endpoint's auth gate, not the admin mount
    assert "text/html" not in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# GET /admin/api/gadi-queue-status
# ---------------------------------------------------------------------------


def test_admin_gadi_queue_status_returns_occupancy(monkeypatch: MonkeyPatch):
    async def fake_get_queue_status(**_kwargs):
        return seqera.GadiQueueStatus(active_workflows=5, max_concurrent_workflows=25)

    monkeypatch.setattr(seqera, "get_queue_status", fake_get_queue_status)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        resp = client.get("/admin/api/gadi-queue-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["activeWorkflows"] == 5
    assert body["maxConcurrentWorkflows"] == 25
    assert body["availableCapacity"] == 20
    assert "checkedAt" in body


def test_admin_gadi_queue_status_requires_admin():
    app = FastAPI()
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        resp = client.get("/admin/api/gadi-queue-status")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/api/gadi-pbs-jobs
# ---------------------------------------------------------------------------


def test_admin_gadi_pbs_jobs_returns_jobs_and_queue_totals(monkeypatch: MonkeyPatch):
    async def fake_get_pbs_jobs(**_kwargs):
        return gadi_pbs_jobs.GadiPbsJobsSnapshot(
            generated_at=datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC),
            jobs=[
                gadi_pbs_jobs.PbsJobStatus(
                    job_id="12345.gadi-pbs",
                    job_name="nf-TASK",
                    state="R",
                    state_label="Running",
                    queue="normal",
                    account="yz52",
                    submitted_at=datetime(2026, 6, 1, 2, 55, 0, tzinfo=UTC),
                    started_at=datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC),
                )
            ],
            queue_totals=[
                gadi_pbs_jobs.QueueTotal(
                    name="normal",
                    mine_queued=0,
                    total_queued=20,
                    mine_running=1,
                    total_running=100,
                    mine_held=0,
                    total_held=0,
                )
            ],
        )

    monkeypatch.setattr(gadi_pbs_jobs, "get_pbs_jobs", fake_get_pbs_jobs)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        resp = client.get("/admin/api/gadi-pbs-jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["generatedAt"].startswith("2026-06-01T03:00:00")
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert job["jobId"] == "12345.gadi-pbs"
    assert job["state"] == "R"
    assert job["stateLabel"] == "Running"
    assert job["queue"] == "normal"
    assert job["account"] == "yz52"

    assert len(body["queueTotals"]) == 1
    total = body["queueTotals"][0]
    assert total["name"] == "normal"
    assert total["mineRunning"] == 1
    assert total["totalRunning"] == 100


def test_admin_gadi_pbs_jobs_requires_admin():
    app = FastAPI()
    app.include_router(system_status_router, prefix="/admin/api")

    with TestClient(app) as client:
        resp = client.get("/admin/api/gadi-pbs-jobs")

    assert resp.status_code == 401


def test_admin_gadi_pbs_jobs_surfaces_push_failure(monkeypatch: MonkeyPatch):
    from app.services.gadi_pbs_jobs import GadiPbsJobsError

    async def fake_get_pbs_jobs(**_kwargs):
        raise GadiPbsJobsError("Could not read Gadi PBS jobs from s3://key: NoSuchKey")

    monkeypatch.setattr(gadi_pbs_jobs, "get_pbs_jobs", fake_get_pbs_jobs)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")

    @app.exception_handler(Exception)
    async def handle_exception(request, exc):  # noqa: ARG001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=500, content={"error": str(exc)})

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/admin/api/gadi-pbs-jobs")

    assert resp.status_code == 500
    assert "Could not read Gadi PBS jobs" in resp.json()["error"]


# ---------------------------------------------------------------------------
# GET /admin/api/system-status/history (downtime incidents)
# ---------------------------------------------------------------------------


def _seed_incident(test_db, *, component, status, hours_ago, ended_hours_ago=None, message=None):
    test_db.add(
        SystemStatusIncident(
            component=component,
            status=status,
            started_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            ended_at=(
                datetime.now(UTC) - timedelta(hours=ended_hours_ago)
                if ended_hours_ago is not None
                else None
            ),
            message=message,
        )
    )
    test_db.commit()


def test_history_requires_admin(client):
    resp = client.get("/admin/api/system-status/history")
    assert resp.status_code == 401


def test_history_returns_per_component_summary_and_incidents(client, test_db):
    client.app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    _seed_incident(
        test_db,
        component="seqera_compute_env",
        status="unhealthy",
        hours_ago=2,
        ended_hours_ago=1,
        message="ERRORED",
    )

    resp = client.get("/admin/api/system-status/history?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["windowHours"] == 24
    assert "since" in body and "until" in body

    by_name = {c["name"]: c for c in body["components"]}
    ce = by_name["seqera_compute_env"]
    assert ce["summary"]["incidentCount"] == 1
    assert ce["summary"]["downtimeSeconds"] == pytest.approx(3600, rel=0.01)
    assert ce["incidents"][0]["message"] == "ERRORED"
    assert ce["incidents"][0]["endedAt"] is not None


def test_history_includes_ongoing_incident(client, test_db):
    client.app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    _seed_incident(test_db, component="seqera_api", status="unhealthy", hours_ago=1)

    resp = client.get("/admin/api/system-status/history?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    api = next(c for c in body["components"] if c["name"] == "seqera_api")
    assert api["incidents"][0]["endedAt"] is None
    assert api["summary"]["incidentCount"] == 1


def test_history_excludes_incidents_outside_window(client, test_db):
    client.app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    _seed_incident(
        test_db, component="seqera_api", status="unhealthy", hours_ago=48, ended_hours_ago=47
    )

    resp = client.get("/admin/api/system-status/history?hours=24")
    assert resp.status_code == 200
    assert resp.json()["components"] == []


def test_history_filters_by_component_query_param(client, test_db):
    client.app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    _seed_incident(test_db, component="seqera_api", status="unhealthy", hours_ago=1)
    _seed_incident(test_db, component="seqera_compute_env", status="unhealthy", hours_ago=1)

    resp = client.get("/admin/api/system-status/history?hours=24&component=seqera_api")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["components"]) == 1
    assert body["components"][0]["name"] == "seqera_api"


def test_history_empty_when_no_incidents_recorded(client):
    client.app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    resp = client.get("/admin/api/system-status/history")
    assert resp.status_code == 200
    assert resp.json()["components"] == []
