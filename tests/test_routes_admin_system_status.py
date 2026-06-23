"""Tests for the admin-only system status API endpoint."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.admin import require_admin_access
from app.routes.system_status import router as system_status_router
from app.services import health
from app.services.health import ProbeResult, SystemStatus

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
            ProbeResult("seqera_api", "healthy", 240, None, None),
            ProbeResult(
                "seqera_compute_env",
                "unhealthy",
                310,
                "Compute environment state: ERRORED (Gadi agent disconnected)",
                {"computeEnv": {"status": "ERRORED"}},
            ),
        ],
    )


def _build_client(monkeypatch) -> TestClient:
    async def fake_get_system_status(*, force_refresh: bool = False):
        return _fake_status()

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)

    app = FastAPI()
    app.dependency_overrides[require_admin_access] = lambda: {"sub": "auth0|admin"}
    app.include_router(system_status_router, prefix="/admin/api")
    return TestClient(app)


def test_admin_system_status_returns_verbose_payload(monkeypatch):
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


def test_admin_system_status_passes_refresh_flag(monkeypatch):
    seen = {}

    async def fake_get_system_status(*, force_refresh: bool = False):
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
