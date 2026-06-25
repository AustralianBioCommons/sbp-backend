"""Tests for the user-facing GET /api/health/components endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.dependencies import get_current_user_id, require_workflow_execution_role
from app.routes.health import router as health_router
from app.services import health
from app.services.health import DEGRADED_USER_MESSAGE, ProbeResult, SystemStatus


def _status(overall: str) -> SystemStatus:
    # The component list is intentionally irrelevant to the coarse projection;
    # only overall_status drives the user-facing response.
    return SystemStatus(
        overall_status=overall,  # type: ignore[arg-type]
        checked_at=datetime(2026, 6, 1, 3, 12, 55, tzinfo=UTC),
        components=[
            ProbeResult("seqera_api", "healthy", 240, None, None),
            ProbeResult(
                "seqera_compute_env",
                overall,  # type: ignore[arg-type]
                310,
                "Compute environment state: ERRORED",
                {"computeEnv": {"status": "ERRORED"}},
            ),
        ],
    )


def _build_client(monkeypatch, overall: str) -> TestClient:
    async def fake_get_system_status(*, force_refresh: bool = False):
        return _status(overall)

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "user-id"
    app.dependency_overrides[require_workflow_execution_role] = lambda: None
    app.include_router(health_router, prefix="/api/health")
    return TestClient(app)


def test_healthy_returns_no_message(monkeypatch):
    client = _build_client(monkeypatch, "healthy")
    resp = client.get("/api/health/components")

    assert resp.status_code == 200
    body = resp.json()
    assert body["overallStatus"] == "healthy"
    assert body["message"] is None
    assert body["checkedAt"].startswith("2026-06-01T03:12:55")
    # Coarse projection does not leak per-component detail.
    assert "components" not in body


def test_degraded_returns_user_message(monkeypatch):
    client = _build_client(monkeypatch, "degraded")
    resp = client.get("/api/health/components")

    assert resp.status_code == 200
    body = resp.json()
    assert body["overallStatus"] == "degraded"
    assert body["message"] == DEGRADED_USER_MESSAGE


def test_unhealthy_returns_user_message(monkeypatch):
    client = _build_client(monkeypatch, "unhealthy")
    resp = client.get("/api/health/components")

    assert resp.status_code == 200
    body = resp.json()
    assert body["overallStatus"] == "unhealthy"
    assert body["message"] == DEGRADED_USER_MESSAGE


def test_passes_refresh_flag(monkeypatch):
    seen = {}

    async def fake_get_system_status(*, force_refresh: bool = False):
        seen["force_refresh"] = force_refresh
        return _status("healthy")

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "user-id"
    app.dependency_overrides[require_workflow_execution_role] = lambda: None
    app.include_router(health_router, prefix="/api/health")

    with TestClient(app) as client:
        client.get("/api/health/components?refresh=true")

    assert seen["force_refresh"] is True


def test_requires_authentication(client):
    """Mounted in the default test app; enforces auth when no token is present.

    The shared ``client`` fixture overrides the auth dependencies, so use a bare
    request against the real app to confirm the route is gated rather than open.
    """
    from app.main import create_app

    app = create_app()
    with TestClient(app) as bare_client:
        resp = bare_client.get("/api/health/components")

    # HTTPBearer raises 403 when the Authorization header is missing.
    assert resp.status_code in (401, 403)
