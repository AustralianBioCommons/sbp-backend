"""Tests for the machine-only GET /api/health/agent endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from app.db.models.core import AppUser
from app.routes.dependencies import get_db
from app.routes.health import agent_router
from app.schemas.health import ProbeResult, SystemStatus
from app.services import health

M2M_CLAIMS = {"sub": "m2m-client@clients", "permissions": ["read:agent-health"]}


def _status(agent_status: str | None, *, message: str | None = None) -> SystemStatus:
    components = [
        ProbeResult(name="seqera_api", status="healthy", latency_ms=120),
        ProbeResult(name="seqera_compute_env", status="healthy", latency_ms=90),
    ]
    if agent_status is not None:
        components.append(
            ProbeResult(
                name="seqera_tower_agent",
                status=agent_status,  # type: ignore[arg-type]
                latency_ms=4200,
                message=message,
                detail={"computeEnv": {"status": "AVAILABLE"}},
            )
        )
    overall = agent_status or "healthy"
    return SystemStatus(
        overall_status=overall,  # type: ignore[arg-type]
        checked_at=datetime(2026, 6, 1, 3, 12, 55, tzinfo=UTC),
        components=components,
    )


def _build_client(monkeypatch, mocker: MockerFixture, test_db, *, agent_status, message=None):
    async def fake_get_system_status(db: object | None = None, **_kwargs) -> SystemStatus:
        return _status(agent_status, message=message)

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)
    mocker.patch("app.routes.dependencies.verify_access_token_claims", return_value=M2M_CLAIMS)

    app = FastAPI()

    def _get_db():
        yield test_db

    app.dependency_overrides[get_db] = _get_db
    app.include_router(agent_router, prefix="/api/health")
    return TestClient(app)


def test_valid_m2m_token_with_permission_returns_200(monkeypatch, mocker: MockerFixture, test_db):
    client = _build_client(monkeypatch, mocker, test_db, agent_status="healthy")
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    assert resp.status_code == 200


def test_healthy_agent_returns_status_healthy(monkeypatch, mocker: MockerFixture, test_db):
    client = _build_client(monkeypatch, mocker, test_db, agent_status="healthy")
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["checkedAt"].startswith("2026-06-01T03:12:55")


def test_unhealthy_agent_returns_status_unhealthy(monkeypatch, mocker: MockerFixture, test_db):
    client = _build_client(
        monkeypatch,
        mocker,
        test_db,
        agent_status="unhealthy",
        message="Tower Agent validation failed: compute env ERRORED",
    )
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["message"] == "Tower Agent validation failed: compute env ERRORED"


def test_missing_agent_component_returns_503(monkeypatch, mocker: MockerFixture, test_db):
    """Agent monitoring disabled -> component absent -> 503, not a fake 'unhealthy'."""
    client = _build_client(monkeypatch, mocker, test_db, agent_status=None)
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    assert resp.status_code == 503


def test_response_does_not_expose_raw_probe_detail(monkeypatch, mocker: MockerFixture, test_db):
    client = _build_client(
        monkeypatch, mocker, test_db, agent_status="unhealthy", message="compute env ERRORED"
    )
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    body = resp.json()
    assert set(body.keys()) == {"status", "checkedAt", "message"}
    assert "detail" not in body
    assert "computeEnv" not in str(body)


def test_token_without_permission_returns_403(monkeypatch, mocker: MockerFixture, test_db):
    async def fake_get_system_status(db: object | None = None, **_kwargs) -> SystemStatus:
        return _status("healthy")

    monkeypatch.setattr(health, "get_system_status", fake_get_system_status)
    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        return_value={"sub": "m2m-client@clients", "permissions": ["read:something-else"]},
    )

    app = FastAPI()

    def _get_db():
        yield test_db

    app.dependency_overrides[get_db] = _get_db
    app.include_router(agent_router, prefix="/api/health")
    client = TestClient(app)

    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})
    assert resp.status_code == 403


def test_missing_token_is_rejected(test_db):
    """No mocking here: hits the real app so HTTPBearer itself enforces auth."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as bare_client:
        resp = bare_client.get("/api/health/agent")

    # HTTPBearer raises 403 when the Authorization header is missing entirely.
    assert resp.status_code in (401, 403)


def test_invalid_token_returns_401(monkeypatch, mocker: MockerFixture, test_db):
    from fastapi import HTTPException

    mocker.patch(
        "app.routes.dependencies.verify_access_token_claims",
        side_effect=HTTPException(status_code=401, detail="Invalid token"),
    )

    app = FastAPI()

    def _get_db():
        yield test_db

    app.dependency_overrides[get_db] = _get_db
    app.include_router(agent_router, prefix="/api/health")
    client = TestClient(app)

    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer bad-token"})
    assert resp.status_code == 401


def test_m2m_request_does_not_create_app_user(monkeypatch, mocker: MockerFixture, test_db):
    client = _build_client(monkeypatch, mocker, test_db, agent_status="healthy")
    resp = client.get("/api/health/agent", headers={"Authorization": "Bearer m2m-token"})

    assert resp.status_code == 200
    assert test_db.query(AppUser).count() == 0
    assert (
        test_db.query(AppUser).filter(AppUser.auth0_user_id == "m2m-client@clients").first()
        is None
    )
