"""Tests for the runtime health probe service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.db.models.system_status import SystemStatusCache, SystemStatusIncident
from app.schemas.health import (
    COMPONENT_COMPUTE_ENV,
    COMPONENT_SEQERA_API,
    COMPONENT_TOWER_AGENT,
)
from app.services import health


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


async def test_results_are_cached_in_database(monkeypatch, test_db):
    calls = {"count": 0}

    async def counting_get(self, url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if url.endswith("/user-info"):
            return _ok_user_info(url)
        return _compute_env_with_status("AVAILABLE")(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", counting_get)

    first = await health.get_system_status(test_db, force_refresh=True)
    after_refresh = calls["count"]
    assert after_refresh == 2  # one probe per component

    second = await health.get_system_status(test_db)  # served from DB cache
    assert calls["count"] == after_refresh  # no new network calls
    assert second.overall_status == first.overall_status
    assert second.checked_at == first.checked_at


async def test_stale_cache_served_when_another_process_refreshes(monkeypatch, test_db):
    async def ok_get(self, url, *args, **kwargs):  # noqa: ANN001
        if url.endswith("/user-info"):
            return _ok_user_info(url)
        return _compute_env_with_status("AVAILABLE")(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", ok_get)

    first = await health.get_system_status(test_db, force_refresh=True)
    row = test_db.get(SystemStatusCache, health._CACHE_KEY)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    test_db.add(row)
    test_db.commit()

    monkeypatch.setattr(health, "_try_acquire_cache_refresh_lock", lambda db: False)

    stale = await health.get_system_status(test_db)
    assert stale.overall_status == first.overall_status
    assert stale.checked_at == first.checked_at


# ---------------------------------------------------------------------------
# Downtime incidents (only degraded/unhealthy periods are recorded)
# ---------------------------------------------------------------------------


def _incidents(db, component=None):
    rows = db.execute(select(SystemStatusIncident)).scalars().all()
    return [r for r in rows if component is None or r.component == component]


async def test_healthy_refresh_writes_no_incidents(monkeypatch, test_db):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    await health.get_system_status(test_db, force_refresh=True)

    assert _incidents(test_db) == []


async def test_going_unhealthy_opens_an_incident(monkeypatch, test_db):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("ERRORED", "Gadi agent disconnected"),
    )
    status = await health.get_system_status(test_db, force_refresh=True)

    rows = _incidents(test_db, COMPONENT_COMPUTE_ENV)
    assert len(rows) == 1
    assert rows[0].status == "unhealthy"
    assert rows[0].ended_at is None
    assert "Gadi agent disconnected" in (rows[0].message or "")
    assert rows[0].started_at == status.checked_at.replace(tzinfo=None)


async def test_recovering_to_healthy_closes_the_incident(monkeypatch, test_db):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("ERRORED"),
    )
    await health.get_system_status(test_db, force_refresh=True)

    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("AVAILABLE"),
    )
    recovered = await health.get_system_status(test_db, force_refresh=True)

    rows = _incidents(test_db, COMPONENT_COMPUTE_ENV)
    assert len(rows) == 1  # closed, not duplicated
    assert rows[0].ended_at == recovered.checked_at.replace(tzinfo=None)


async def test_staying_unhealthy_does_not_open_a_second_incident(monkeypatch, test_db):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("ERRORED", "first error"),
    )
    await health.get_system_status(test_db, force_refresh=True)

    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("ERRORED", "still erroring"),
    )
    await health.get_system_status(test_db, force_refresh=True)

    rows = _incidents(test_db, COMPONENT_COMPUTE_ENV)
    assert len(rows) == 1
    assert rows[0].ended_at is None
    # The message on the still-open incident is refreshed to the latest probe result.
    assert rows[0].message == "Compute environment state: ERRORED (still erroring)"


async def test_severity_change_splits_the_incident(monkeypatch, test_db):
    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("CREATING"),  # degraded
    )
    await health.get_system_status(test_db, force_refresh=True)

    _mock_response(
        monkeypatch,
        user_info=_ok_user_info,
        compute_env=_compute_env_with_status("ERRORED"),  # unhealthy
    )
    await health.get_system_status(test_db, force_refresh=True)

    rows = sorted(_incidents(test_db, COMPONENT_COMPUTE_ENV), key=lambda r: r.started_at)
    assert len(rows) == 2
    assert rows[0].status == "degraded"
    assert rows[0].ended_at is not None  # closed when severity changed
    assert rows[1].status == "unhealthy"
    assert rows[1].ended_at is None


def test_get_incidents_filters_by_window_and_component(test_db):
    now = datetime.now(UTC)
    long_ago = SystemStatusIncident(
        component=COMPONENT_SEQERA_API,
        status="unhealthy",
        started_at=now - timedelta(hours=5),
        ended_at=now - timedelta(hours=4, minutes=55),
    )
    recent_api = SystemStatusIncident(
        component=COMPONENT_SEQERA_API,
        status="unhealthy",
        started_at=now - timedelta(minutes=30),
        ended_at=now - timedelta(minutes=25),
    )
    ongoing_ce = SystemStatusIncident(
        component=COMPONENT_COMPUTE_ENV,
        status="degraded",
        started_at=now - timedelta(minutes=10),
        ended_at=None,
    )
    test_db.add_all([long_ago, recent_api, ongoing_ce])
    test_db.commit()

    since = now - timedelta(hours=1)
    all_recent = health.get_incidents(test_db, since=since, until=now)
    assert {row.component for row in all_recent} == {COMPONENT_SEQERA_API, COMPONENT_COMPUTE_ENV}

    api_only = health.get_incidents(test_db, since=since, until=now, component=COMPONENT_SEQERA_API)
    assert len(api_only) == 1
    assert api_only[0].ended_at is not None


def test_summarize_downtime_clips_to_window():
    now = datetime.now(UTC)
    since = now - timedelta(hours=1)
    incidents = [
        # Started before the window, ended inside it -> clipped at `since`.
        SystemStatusIncident(
            component="x",
            status="unhealthy",
            started_at=now - timedelta(hours=2),
            ended_at=since + timedelta(minutes=10),
        ),
        # Still ongoing -> counts through `until`.
        SystemStatusIncident(
            component="x",
            status="degraded",
            started_at=now - timedelta(minutes=5),
            ended_at=None,
        ),
    ]
    summary = health._summarize_downtime(incidents, since=since, until=now)

    assert summary["incidentCount"] == 2
    assert summary["downtimeSeconds"] == 10 * 60 + 5 * 60
    assert summary["uptimePercent"] == round((3600 - 900) / 3600 * 100, 2)


def test_summarize_downtime_no_incidents_is_full_uptime():
    now = datetime.now(UTC)
    summary = health._summarize_downtime([], since=now - timedelta(hours=1), until=now)
    assert summary == {"incidentCount": 0, "downtimeSeconds": 0.0, "uptimePercent": 100.0}


def test_to_downtime_dict_groups_by_component():
    now = datetime.now(UTC)
    since = now - timedelta(hours=24)
    incidents = [
        SystemStatusIncident(
            component=COMPONENT_SEQERA_API,
            status="unhealthy",
            started_at=since + timedelta(hours=1),
            ended_at=since + timedelta(hours=2),
        ),
        SystemStatusIncident(
            component=COMPONENT_COMPUTE_ENV,
            status="degraded",
            started_at=since + timedelta(hours=3),
            ended_at=None,
            message="still starting up",
        ),
    ]
    result = health.to_downtime_dict(incidents, since=since, until=now, hours=24)

    assert result["windowHours"] == 24
    assert result["since"] == since
    assert result["until"] == now
    by_name = {c["name"]: c for c in result["components"]}
    assert by_name[COMPONENT_SEQERA_API]["summary"]["incidentCount"] == 1
    assert by_name[COMPONENT_SEQERA_API]["incidents"][0]["endedAt"] == since + timedelta(hours=2)
    assert by_name[COMPONENT_COMPUTE_ENV]["incidents"][0]["message"] == "still starting up"


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
