"""Tests for the runtime health probe service."""

from __future__ import annotations

import httpx
import pytest

from app.schemas.health import (
    COMPONENT_COMPUTE_ENV,
    COMPONENT_SEQERA_API,
    COMPONENT_TOWER_AGENT,
)
from app.services import health


@pytest.fixture(autouse=True)
def _clear_status_cache():
    """Each test starts with an empty probe cache."""
    health._status_cache.clear()
    yield
    health._status_cache.clear()


def _component(status: health.SystemStatus, name: str) -> health.ProbeResult:
    return next(c for c in status.components if c.name == name)


def _mock_response(monkeypatch, *, user_info, compute_env):
    """Patch httpx.AsyncClient.get to dispatch on URL."""

    async def fake_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/user-info"):
            return user_info(url)
        if "/compute-envs/" in url:
            return compute_env(url)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def _ok_user_info(url):
    return httpx.Response(
        200,
        json={"user": {"id": 1, "userName": "sbp-svc", "email": "svc@example.org"}},
        request=httpx.Request("GET", url),
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
        user_info=_ok_user_info,
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
        user_info=_ok_user_info,
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
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("CREATING"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "degraded"
    assert _component(status, COMPONENT_COMPUTE_ENV).status == "degraded"


async def test_compute_env_unknown_state_is_degraded(monkeypatch):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("SOMETHING_NEW"),
    )
    status = await health.get_system_status(force_refresh=True)
    assert _component(status, COMPONENT_COMPUTE_ENV).status == "degraded"


async def test_seqera_api_non_2xx_is_unhealthy(monkeypatch):
    def bad_user_info(url):
        return httpx.Response(502, text="Bad Gateway", request=httpx.Request("GET", url))

    _mock_response(
        monkeypatch,
        user_info=bad_user_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)

    assert status.overall_status == "unhealthy"
    api = _component(status, COMPONENT_SEQERA_API)
    assert api.status == "unhealthy"
    assert "502" in (api.message or "")
    assert api.detail is not None and api.detail["statusCode"] == 502


async def test_seqera_api_token_rejected_is_unhealthy(monkeypatch):
    # An authenticated /user-info call surfaces a bad/expired token as 401/403.
    def rejected(url):
        return httpx.Response(401, text="Unauthorized", request=httpx.Request("GET", url))

    _mock_response(
        monkeypatch,
        user_info=rejected,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)

    api = _component(status, COMPONENT_SEQERA_API)
    assert api.status == "unhealthy"
    assert "token" in (api.message or "").lower()
    assert "SEQERA_ACCESS_TOKEN" in (api.message or "")
    assert api.detail is not None and api.detail["statusCode"] == 401


async def test_seqera_api_timeout_is_unhealthy(monkeypatch):
    async def timeout_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/user-info"):
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
        user_info=_ok_user_info,
        compute_env=bad_compute,
    )
    status = await health.get_system_status(force_refresh=True)
    ce = _component(status, COMPONENT_COMPUTE_ENV)
    assert ce.status == "unhealthy"
    assert "404" in (ce.message or "")
    # A 403/404 hints at a WORK_SPACE / COMPUTE_ID / token misconfiguration.
    assert "WORK_SPACE" in (ce.message or "")


async def test_results_are_cached(monkeypatch):
    calls = {"count": 0}

    async def counting_get(self, url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if url.endswith("/user-info"):
            return _ok_user_info(url)
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
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)
    admin_dict = health.to_admin_dict(status)
    public_dict = health.to_public_dict(status)

    assert set(admin_dict) == {"overallStatus", "checkedAt", "components", "cloudwatchLogGroupUrl"}
    assert "detail" in admin_dict["components"][0]
    # Coarse projection must not leak raw detail.
    assert "detail" not in public_dict["components"][0]


# ---------------------------------------------------------------------------
# Tower Agent probe (opt-in create->poll->delete liveness check)
# ---------------------------------------------------------------------------

_SOURCE_ENV = {
    "platform": "altair-platform",
    "config": {"workDir": "/scratch/sbp"},
    "credentialsId": "cred-tw-agent",
    "status": "AVAILABLE",
}


def _install_agent_mock(
    monkeypatch,
    *,
    poll_states,
    created_id="probe-ce-123",
    create_status=200,
    delete_status=200,
    src_env=None,
):
    """Patch get/post/delete to simulate the clone->create->poll->delete cycle."""
    monkeypatch.setenv("ENABLE_AGENT_HEALTHCHECK", "true")
    monkeypatch.setattr(health, "_AGENT_PROBE_POLL_INTERVAL_SECONDS", 0)
    source = src_env if src_env is not None else _SOURCE_ENV
    state = {"poll_i": 0}
    calls = {"created": False, "deleted": False, "create_body": None}

    async def fake_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/user-info"):
            return _ok_user_info(url)
        if url.endswith(f"/compute-envs/{created_id}"):
            i = min(state["poll_i"], len(poll_states) - 1)
            state["poll_i"] += 1
            return httpx.Response(
                200,
                json={"computeEnv": {"id": created_id, "status": poll_states[i]}},
                request=httpx.Request("GET", url),
            )
        if "/compute-envs/" in url:  # source clone GET + compute_env probe
            return httpx.Response(
                200, json={"computeEnv": source}, request=httpx.Request("GET", url)
            )
        raise AssertionError(f"unexpected GET: {url}")

    async def fake_post(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/compute-envs"):
            calls["created"] = True
            calls["create_body"] = kwargs.get("json")
            if create_status >= 400:
                return httpx.Response(
                    create_status, text="rejected", request=httpx.Request("POST", url)
                )
            return httpx.Response(
                create_status, json={"computeEnvId": created_id}, request=httpx.Request("POST", url)
            )
        raise AssertionError(f"unexpected POST: {url}")

    async def fake_delete(self, url, *args, **kwargs):  # noqa: ANN001
        calls["deleted"] = True
        return httpx.Response(delete_status, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)
    return calls


async def test_agent_probe_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_AGENT_HEALTHCHECK", raising=False)
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    status = await health.get_system_status(force_refresh=True)
    names = {c.name for c in status.components}
    assert COMPONENT_TOWER_AGENT not in names
    assert names == {COMPONENT_SEQERA_API, COMPONENT_COMPUTE_ENV}


async def test_agent_probe_healthy_and_cleans_up(monkeypatch):
    calls = _install_agent_mock(monkeypatch, poll_states=["AVAILABLE"])
    status = await health.get_system_status(force_refresh=True)

    agent = _component(status, COMPONENT_TOWER_AGENT)
    assert agent.status == "healthy"
    assert calls["created"] is True
    assert calls["deleted"] is True  # throwaway env always cleaned up
    # The created env cloned the source platform/config/credential.
    ce = calls["create_body"]["computeEnv"]
    assert ce["platform"] == _SOURCE_ENV["platform"]
    assert ce["credentialsId"] == _SOURCE_ENV["credentialsId"]
    assert ce["name"].startswith("sbp-agent-healthcheck-")


async def test_agent_probe_unhealthy_when_validation_fails(monkeypatch):
    calls = _install_agent_mock(monkeypatch, poll_states=["CREATING", "ERRORED"])
    status = await health.get_system_status(force_refresh=True)

    agent = _component(status, COMPONENT_TOWER_AGENT)
    assert agent.status == "unhealthy"
    assert "ERRORED" in (agent.message or "")
    assert status.overall_status == "unhealthy"
    assert calls["deleted"] is True  # cleaned up even on failure


async def test_agent_probe_create_rejected_is_unhealthy(monkeypatch):
    calls = _install_agent_mock(monkeypatch, poll_states=["AVAILABLE"], create_status=400)
    status = await health.get_system_status(force_refresh=True)

    agent = _component(status, COMPONENT_TOWER_AGENT)
    assert agent.status == "unhealthy"
    assert "rejected" in (agent.message or "").lower()
    # Nothing was created, so there is nothing to delete.
    assert calls["deleted"] is False


async def test_agent_probe_timeout_is_degraded(monkeypatch):
    monkeypatch.setattr(health, "_AGENT_PROBE_TIMEOUT_SECONDS", 0)
    calls = _install_agent_mock(monkeypatch, poll_states=["CREATING"])
    status = await health.get_system_status(force_refresh=True)

    agent = _component(status, COMPONENT_TOWER_AGENT)
    assert agent.status == "degraded"
    assert "not confirmed" in (agent.message or "").lower()
    assert calls["deleted"] is True
