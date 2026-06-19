"""Tests for the runtime health probe service."""

from __future__ import annotations

import httpx
import pytest

from app.schemas.health import COMPONENT_COMPUTE_ENV, COMPONENT_SEQERA_API
from app.services import health


@pytest.fixture(autouse=True)
def _clear_status_cache():
    """Each test starts with an empty probe cache."""
    health._status_cache.clear()
    yield
    health._status_cache.clear()


def _component(status: health.SystemStatus, name: str) -> health.ProbeResult:
    return next(c for c in status.components if c.name == name)


def _mock_response(monkeypatch, *, service_info, compute_env):
    """Patch httpx.AsyncClient.get to dispatch on URL."""

    async def fake_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/service-info"):
            return service_info(url)
        if "/compute-envs/" in url:
            return compute_env(url)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def _ok_service_info(url):
    return httpx.Response(
        200, json={"serviceInfo": {"version": "23.1.0"}}, request=httpx.Request("GET", url)
    )


def _compute_env_with_status(state, message=None):
    def _factory(url):
        env: dict = {"id": "ce-1", "name": "gadi", "status": state}
        if message is not None:
            env["message"] = message
        return httpx.Response(200, json={"computeEnv": env}, request=httpx.Request("GET", url))

    return _factory


async def test_all_healthy(monkeypatch):
    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "healthy"
    api = _component(status, COMPONENT_SEQERA_API)
    ce = _component(status, COMPONENT_COMPUTE_ENV)
    assert api.status == "healthy"
    assert api.message is None
    assert ce.status == "healthy"
    assert ce.latency_ms is not None


async def test_compute_env_errored_is_unhealthy(monkeypatch):
    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=_compute_env_with_status("ERRORED", "Gadi agent disconnected"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "unhealthy"
    ce = _component(status, COMPONENT_COMPUTE_ENV)
    assert ce.status == "unhealthy"
    assert "ERRORED" in (ce.message or "")
    assert "Gadi agent disconnected" in (ce.message or "")
    assert ce.detail is not None and ce.detail["computeEnv"]["status"] == "ERRORED"


async def test_compute_env_creating_is_degraded(monkeypatch):
    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=_compute_env_with_status("CREATING"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "degraded"
    assert _component(status, COMPONENT_COMPUTE_ENV).status == "degraded"


async def test_compute_env_unknown_state_is_degraded(monkeypatch):
    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=_compute_env_with_status("SOMETHING_NEW"),
    )
    status = await health.get_system_status(force_refresh=True)
    assert _component(status, COMPONENT_COMPUTE_ENV).status == "degraded"


async def test_seqera_api_non_2xx_is_unhealthy(monkeypatch):
    def bad_service_info(url):
        return httpx.Response(502, text="Bad Gateway", request=httpx.Request("GET", url))

    _mock_response(
        monkeypatch,
        service_info=bad_service_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "unhealthy"
    api = _component(status, COMPONENT_SEQERA_API)
    assert api.status == "unhealthy"
    assert "502" in (api.message or "")
    assert api.detail is not None and api.detail["statusCode"] == 502


async def test_seqera_api_timeout_is_unhealthy(monkeypatch):
    async def timeout_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/service-info"):
            raise httpx.TimeoutException("timed out")
        return _compute_env_with_status("AVAILABLE")(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", timeout_get)
    status = await health.get_system_status(force_refresh=True)

    api = _component(status, COMPONENT_SEQERA_API)
    assert api.status == "unhealthy"
    assert api.detail is not None and api.detail["error"] == "timeout"


async def test_compute_env_read_failure_is_unhealthy(monkeypatch):
    def bad_compute(url):
        return httpx.Response(404, text="not found", request=httpx.Request("GET", url))

    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=bad_compute,
    )
    status = await health.get_system_status(force_refresh=True)
    ce = _component(status, COMPONENT_COMPUTE_ENV)
    assert ce.status == "unhealthy"
    assert "404" in (ce.message or "")


async def test_results_are_cached(monkeypatch):
    calls = {"count": 0}

    async def counting_get(self, url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if url.endswith("/service-info"):
            return _ok_service_info(url)
        return _compute_env_with_status("AVAILABLE")(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", counting_get)

    first = await health.get_system_status(force_refresh=True)
    after_refresh = calls["count"]
    assert after_refresh == 2  # one probe per component

    second = await health.get_system_status()  # served from cache
    assert calls["count"] == after_refresh  # no new network calls
    assert second is first


def test_overall_status_aggregation():
    assert health._worst(["healthy", "healthy"]) == "healthy"
    assert health._worst(["healthy", "degraded"]) == "degraded"
    assert health._worst(["degraded", "unhealthy"]) == "unhealthy"


def test_cloudwatch_url_built_when_configured(monkeypatch):
    monkeypatch.setenv("SBP_BACKEND_LOG_GROUP", "/ecs/sbp-backend")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    url = health._cloudwatch_log_group_url()
    assert url is not None
    assert "ap-southeast-2" in url
    assert "log-group" in url


def test_cloudwatch_url_none_when_unset(monkeypatch):
    monkeypatch.delenv("SBP_BACKEND_LOG_GROUP", raising=False)
    assert health._cloudwatch_log_group_url() is None


async def test_to_admin_dict_shape(monkeypatch):
    _mock_response(
        monkeypatch,
        service_info=_ok_service_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)
    admin_dict = health.to_admin_dict(status)
    public_dict = health.to_public_dict(status)

    assert set(admin_dict) == {"overallStatus", "checkedAt", "components", "cloudwatchLogGroupUrl"}
    assert "detail" in admin_dict["components"][0]
    # Coarse projection must not leak raw detail.
    assert "detail" not in public_dict["components"][0]
