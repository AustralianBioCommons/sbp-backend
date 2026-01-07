"""Tests for the main FastAPI application."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_create_app_success():
    """Test that create_app creates a valid FastAPI instance."""
    from app.main import create_app

    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "SBP Portal Backend"
    assert app.version == "1.0.0"


def test_create_app_missing_allowed_origins():
    """Test that create_app raises error when ALLOWED_ORIGINS is missing."""
    from app.main import create_app

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS environment variable is required"):
            create_app()


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
    route_paths = [route.path for route in app.routes]

    assert "/api/workflows/launch" in route_paths
    assert "/api/workflows/runs" in route_paths


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
