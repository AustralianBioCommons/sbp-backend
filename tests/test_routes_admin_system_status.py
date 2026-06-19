"""Tests for the admin-only system status API endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.admin import _mount_system_status_api, require_admin_access
from app.services import health
from app.services.health import ProbeResult, SystemStatus


def _fake_status() -> SystemStatus:
    return SystemStatus(
        overall_status="unhealthy",
        checked_at=datetime(2026, 6, 1, 3, 12, 55, tzinfo=timezone.utc),
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
    _mount_system_status_api(app)
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
    _mount_system_status_api(app)

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
    _mount_system_status_api(app)

    with TestClient(app) as client:
        client.get("/admin/api/system-status?refresh=true")

    assert seen["force_refresh"] is True
