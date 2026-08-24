"""Tests for the main FastAPI application."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_create_app_success():
    """Test that create_app creates a valid FastAPI instance."""
    from app.main import create_app

    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "SBP Portal Backend"
    assert app.version == "1.0.0"


def test_create_app_missing_allowed_origins(monkeypatch, mocker, test_get_settings):
    """Test that create_app raises error when ALLOWED_ORIGINS is missing."""
    from app.main import create_app

    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    mocker.patch("app.main.get_settings", test_get_settings)

    with pytest.raises(ValidationError) as exc:
        create_app()

    field_error = exc.value.errors()[0]
    assert field_error["loc"] == ("allowed_origins",)
    assert field_error["type"] == "missing"


def test_health_endpoint(client: TestClient):
    """Test the /health endpoint returns correct response."""
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_cors_middleware_configured(app: FastAPI):
    """Test that CORS middleware is properly configured."""
    # Check that middleware is added
    middleware_found = False
    for middleware in app.user_middleware:
        if "CORSMiddleware" in str(middleware):
            middleware_found = True
            break

    assert middleware_found, "CORS middleware should be configured"


def test_workflow_router_included(app: FastAPI):
    """Test that workflow router is included with correct prefix."""
    assert app.url_path_for("launch_workflow") == "/api/workflows/launch"
    assert app.url_path_for("list_jobs") == "/api/jobs"
    assert app.url_path_for("get_my_credit") == "/api/users/me/credit"


def test_admin_debug_router_included_when_enabled(mocker, mock_settings):
    """Test that debug admin endpoints are mounted when ENABLE_DB_ADMIN=true."""
    from app.main import create_app

    mock_settings.enable_db_admin = True
    mocker.patch("app.main.get_settings", return_value=mock_settings)
    app = create_app()

    paths = app.openapi()["paths"]
    assert "/admin/debug/s3-objects" in paths
    assert "/admin/debug/run-inputs" in paths
    assert "/admin/debug/run-outputs" in paths


def test_exception_handler(client: TestClient):
    """Test that global exception handler works."""
    # Try to access a non-existent endpoint
    response = client.get("/nonexistent")

    # Should return 404 but not crash
    assert response.status_code == 404


def test_cors_allowed_origins_parsing():
    """Test that ALLOWED_ORIGINS is correctly parsed from environment."""
    from app.main import create_app

    with patch.dict(
        os.environ, {"ALLOWED_ORIGINS": "http://localhost:3000, http://localhost:4200"}
    ):
        app = create_app()

        cors_options = next(mw.kwargs for mw in app.user_middleware if "CORSMiddleware" in str(mw))

        # Verify the parsed origins are correctly set in the middleware
        allowed_origins = cors_options["allow_origins"]

        # Check that both origins are present after parsing
        assert "http://localhost:3000" in allowed_origins
        assert "http://localhost:4200" in allowed_origins
        assert len(allowed_origins) == 2


def test_cors_allowed_origins_with_empty_values():
    """Test that empty values in ALLOWED_ORIGINS are filtered out."""
    from app.main import create_app

    with patch.dict(
        os.environ, {"ALLOWED_ORIGINS": "http://localhost:3000,,  , http://localhost:4200"}
    ):
        app = create_app()

        cors_options = next(mw.kwargs for mw in app.user_middleware if "CORSMiddleware" in str(mw))

        # Verify empty values and whitespace are filtered out
        allowed_origins = cors_options["allow_origins"]

        # Should only have 2 valid origins (empty strings filtered out)
        assert "http://localhost:3000" in allowed_origins
        assert "http://localhost:4200" in allowed_origins
        assert len(allowed_origins) == 2
        assert "" not in allowed_origins
