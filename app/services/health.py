"""Runtime health probes for the two components workflow submission depends on.

Two probes, both reached through the Seqera Platform API:

1. ``seqera_api`` — can we talk to the Seqera Platform at all?
2. ``seqera_compute_env`` — is the Gadi-backed compute environment AVAILABLE?
   Seqera reports the Tower Agent connection state via the compute-env ``status``
   field, which is the closest proxy we have for Gadi-side health.

Results are cached for a short TTL so polling (admin dashboard + portal banner)
stays cheap; ``asyncio.Lock`` provides stampede protection on cache misses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from cachetools import TTLCache  # type: ignore[import-untyped]

from ..schemas.health import (
    COMPONENT_COMPUTE_ENV,
    COMPONENT_SEQERA_API,
    COMPONENT_TOWER_AGENT,
    HealthStatus,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)

# Probe network budget. Kept short so a hung Seqera call cannot stall the
# dashboard / submission pre-flight check.
PROBE_TIMEOUT_SECONDS = 5.0

# Tower Agent liveness probe (opt-in via ENABLE_AGENT_HEALTHCHECK). This actively
# verifies the agent by cloning the monitored compute env, creating a throwaway
# copy (which forces Seqera to validate the agent connection), reading its status,
# then deleting it. It mutates Seqera state, so it is off by default.
_AGENT_HEALTHCHECK_NAME_PREFIX = "sbp-agent-healthcheck-"
# Total time budget to wait for the throwaway env to reach a terminal state.
_AGENT_PROBE_TIMEOUT_SECONDS = float(os.getenv("HEALTHCHECK_AGENT_TIMEOUT_SECONDS", "20"))
_AGENT_PROBE_POLL_INTERVAL_SECONDS = 2.0
# Best-effort retries when deleting the throwaway env, so we don't leak resources.
_AGENT_PROBE_DELETE_ATTEMPTS = 3

# Cache the whole computed status. 30s TTL keeps repeated polls (admin every 30s,
# portal every 60s) off the Seqera API while staying fresh enough to be useful.
_CACHE_TTL_SECONDS = float(os.getenv("HEALTH_CACHE_TTL_SECONDS", "30"))
_CACHE_KEY = "system_status"
_status_cache: TTLCache[str, SystemStatus] = TTLCache(maxsize=1, ttl=_CACHE_TTL_SECONDS)

# Lazily created so the module imports cleanly outside a running event loop.
_cache_lock: Any = None

# Stale-while-revalidate state. ``_last_status`` retains the most recent result
# beyond the TTL so an expired-cache read can be served instantly while a refresh
# runs in the background — callers never block on the (~2s) probes except on a
# cold start. ``_refresh_task`` guards against launching duplicate refreshes.
_last_status: SystemStatus | None = None
_refresh_task: Any = None

# Seqera compute-env state -> our normalized health bucket.
_COMPUTE_ENV_STATE_MAP: dict[str, HealthStatus] = {
    "AVAILABLE": "healthy",
    "CREATING": "degraded",
    "ERRORED": "unhealthy",
    "OFFLINE": "unhealthy",
    "INVALID": "unhealthy",
}


@dataclass
class ProbeResult:
    """Outcome of probing a single component."""

    name: str
    status: HealthStatus
    latency_ms: int | None
    message: str | None
    detail: dict[str, Any] | None


@dataclass
class SystemStatus:
    """Aggregated status across all probed components."""

    overall_status: HealthStatus
    checked_at: datetime
    components: list[ProbeResult]


def _get_lock() -> Any:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _agent_probe_enabled() -> bool:
    return os.getenv("ENABLE_AGENT_HEALTHCHECK", "false").strip().lower() in {"1", "true", "yes"}


def _required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise SeqeraConfigurationError(f"Missing required environment variable: {key}")
    return value


def _seqera_headers() -> dict[str, str]:
    token = _required_env("SEQERA_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _worst(statuses: list[HealthStatus]) -> HealthStatus:
    """unhealthy if any unhealthy, else degraded if any degraded, else healthy."""
    if any(s == "unhealthy" for s in statuses):
        return "unhealthy"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    return "healthy"


def _truncate(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def _probe_seqera_api() -> ProbeResult:
    """Probe Seqera Platform reachability *and* credential validity via /user-info.

    ``/user-info`` is an authenticated endpoint, so a 2xx confirms three things at
    once: the platform is reachable, ``SEQERA_API_URL`` is correct, and our
    ``SEQERA_ACCESS_TOKEN`` is accepted. We treat 401/403 specially so a rejected
    or expired token is reported as a credential problem rather than a generic
    outage. (``WORK_SPACE`` is validated separately by the workspace-scoped
    compute-env probe below.) Non-2xx or timeout -> unhealthy, which lets us
    distinguish a Seqera-side / credential problem from a compute-env problem.
    """
    name = COMPONENT_SEQERA_API
    try:
        api_url = _required_env("SEQERA_API_URL").rstrip("/")
        headers = _seqera_headers()
    except SeqeraConfigurationError as exc:
        return ProbeResult(name, "unhealthy", None, str(exc), {"error": str(exc)})

    url = f"{api_url}/user-info"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Seqera API did not respond within {int(PROBE_TIMEOUT_SECONDS)}s",
            {"error": "timeout", "url": url},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Seqera API unreachable: {exc}",
            {"error": str(exc), "url": url},
        )

    if response.status_code in (401, 403):
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Seqera rejected the access token (HTTP {response.status_code}); "
            "check SEQERA_ACCESS_TOKEN",
            {
                "statusCode": response.status_code,
                "responseBody": _truncate(response.text),
                "url": url,
            },
        )

    if response.is_error:
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Seqera API returned HTTP {response.status_code}",
            {
                "statusCode": response.status_code,
                "responseBody": _truncate(response.text),
                "url": url,
            },
        )

    return ProbeResult(name, "healthy", latency_ms, None, None)


async def _probe_compute_env() -> ProbeResult:
    """Probe the Gadi-backed compute environment status via the Seqera API.

    Reads ``GET /compute-envs/{COMPUTE_ID}?workspaceId={WORK_SPACE}`` and maps the
    ``computeEnv.status`` field, which reflects the Tower Agent connection state.
    """
    name = COMPONENT_COMPUTE_ENV
    try:
        api_url = _required_env("SEQERA_API_URL").rstrip("/")
        compute_id = _required_env("COMPUTE_ID")
        workspace_id = _required_env("WORK_SPACE")
        headers = _seqera_headers()
    except SeqeraConfigurationError as exc:
        return ProbeResult(name, "unhealthy", None, str(exc), {"error": str(exc)})

    url = f"{api_url}/compute-envs/{compute_id}"
    params = {"workspaceId": workspace_id}
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers, params=params)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Compute environment check did not respond within {int(PROBE_TIMEOUT_SECONDS)}s",
            {"error": "timeout", "url": url},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Compute environment unreachable: {exc}",
            {"error": str(exc), "url": url},
        )

    if response.is_error:
        # If we cannot read the compute env, we cannot vouch for the agent. A
        # 403/404 here typically means a wrong WORK_SPACE / COMPUTE_ID or a token
        # without access to them, so call that out explicitly.
        error_message = f"Could not read compute environment (HTTP {response.status_code})"
        if response.status_code in (403, 404):
            error_message = f"{error_message}; check COMPUTE_ID, WORK_SPACE, and the access token"
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            error_message,
            {
                "statusCode": response.status_code,
                "responseBody": _truncate(response.text),
                "url": url,
            },
        )

    body = response.json()
    compute_env = body.get("computeEnv", body) if isinstance(body, dict) else {}
    state = str(compute_env.get("status", "")).upper()
    # Unknown/empty states are treated cautiously as degraded, not healthy.
    status: HealthStatus = _COMPUTE_ENV_STATE_MAP.get(state, "degraded")

    env_message = compute_env.get("message")
    message: str | None = None
    if status != "healthy":
        message = f"Compute environment state: {state or 'UNKNOWN'}"
        if isinstance(env_message, str) and env_message.strip():
            message = f"{message} ({env_message.strip()})"

    return ProbeResult(name, status, latency_ms, message, {"computeEnv": compute_env})


async def _delete_compute_env(
    api_url: str, headers: dict[str, str], params: dict[str, str], compute_env_id: str
) -> bool:
    """Best-effort delete of a throwaway compute env, with a few retries.

    Returns True if the env was deleted (or already gone). Never raises, so it is
    safe to call from a ``finally`` block.
    """
    url = f"{api_url}/compute-envs/{compute_env_id}"
    for attempt in range(_AGENT_PROBE_DELETE_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                resp = await client.delete(url, headers=headers, params=params)
            if resp.status_code == 404 or not resp.is_error:
                return True
        except httpx.HTTPError:
            pass
        if attempt < _AGENT_PROBE_DELETE_ATTEMPTS - 1:
            await asyncio.sleep(1.0)
    logger.error(
        "Failed to delete health-check compute env %s after %d attempts; "
        "it may need manual cleanup in Seqera",
        compute_env_id,
        _AGENT_PROBE_DELETE_ATTEMPTS,
    )
    return False


async def _poll_compute_env_state(
    client: httpx.AsyncClient,
    api_url: str,
    compute_env_id: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> tuple[HealthStatus, str | None, dict[str, Any]]:
    """Poll a freshly-created compute env until it reaches a terminal state.

    AVAILABLE -> healthy (agent answered and the env validated), ERRORED/INVALID
    -> unhealthy (agent unreachable / validation failed), still CREATING at the
    deadline -> degraded (could not confirm in time).
    """
    url = f"{api_url}/compute-envs/{compute_env_id}"
    deadline = time.perf_counter() + _AGENT_PROBE_TIMEOUT_SECONDS
    last_state = "UNKNOWN"
    while True:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 404:
            # The env vanished (e.g. concurrent cleanup); cannot confirm.
            return "degraded", "Health-check compute env disappeared before validation", {}
        if not resp.is_error:
            env = resp.json().get("computeEnv", {})
            last_state = str(env.get("status", "")).upper() or "UNKNOWN"
            env_message = env.get("message")
            if last_state == "AVAILABLE":
                return "healthy", None, {"computeEnv": env}
            if last_state in ("ERRORED", "INVALID", "OFFLINE"):
                msg = f"Tower Agent validation failed: compute env {last_state}"
                if isinstance(env_message, str) and env_message.strip():
                    msg = f"{msg} ({env_message.strip()})"
                return "unhealthy", msg, {"computeEnv": env}
        if time.perf_counter() >= deadline:
            return (
                "degraded",
                f"Tower Agent not confirmed within {int(_AGENT_PROBE_TIMEOUT_SECONDS)}s "
                f"(compute env still {last_state})",
                {"lastState": last_state},
            )
        await asyncio.sleep(_AGENT_PROBE_POLL_INTERVAL_SECONDS)


async def _probe_tower_agent() -> ProbeResult:
    """Actively verify Tower Agent liveness via a clone-create-delete cycle.

    Clones the monitored compute env (``COMPUTE_ID``) — reusing its platform,
    config and tw-agent credential — to create a throwaway copy. Creating an
    agent-backed env forces Seqera to validate the agent connection, so the
    resulting env status is a live liveness signal that the plain compute-env
    ``status`` cannot give. The throwaway env is always deleted afterwards.
    """
    name = COMPONENT_TOWER_AGENT
    try:
        api_url = _required_env("SEQERA_API_URL").rstrip("/")
        compute_id = _required_env("COMPUTE_ID")
        workspace_id = _required_env("WORK_SPACE")
        headers = _seqera_headers()
    except SeqeraConfigurationError as exc:
        return ProbeResult(name, "unhealthy", None, str(exc), {"error": str(exc)})

    params = {"workspaceId": workspace_id}
    post_headers = {**headers, "Content-Type": "application/json"}
    start = time.perf_counter()
    created_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            # 1. Clone the monitored compute env's platform / config / credential.
            src = await client.get(
                f"{api_url}/compute-envs/{compute_id}", headers=headers, params=params
            )
            if src.is_error:
                return ProbeResult(
                    name,
                    "unhealthy",
                    int((time.perf_counter() - start) * 1000),
                    f"Could not read source compute env to clone (HTTP {src.status_code})",
                    {"statusCode": src.status_code, "responseBody": _truncate(src.text)},
                )
            src_env = src.json().get("computeEnv", {})
            platform = src_env.get("platform")
            config = src_env.get("config")
            credentials_id = src_env.get("credentialsId")
            if not (platform and config and credentials_id):
                return ProbeResult(
                    name,
                    "degraded",
                    int((time.perf_counter() - start) * 1000),
                    "Source compute env is missing platform/config/credentialsId to clone",
                    {"platform": platform, "hasConfig": bool(config)},
                )

            # 2. Create a throwaway copy. This forces agent validation.
            probe_name = (
                f"{_AGENT_HEALTHCHECK_NAME_PREFIX}{int(time.time())}-{secrets.token_hex(3)}"
            )
            body = {
                "computeEnv": {
                    "name": probe_name,
                    "platform": platform,
                    "config": config,
                    "credentialsId": credentials_id,
                }
            }
            create = await client.post(
                f"{api_url}/compute-envs", headers=post_headers, params=params, json=body
            )
            if create.is_error:
                return ProbeResult(
                    name,
                    "unhealthy",
                    int((time.perf_counter() - start) * 1000),
                    f"Tower Agent health-check env creation was rejected "
                    f"(HTTP {create.status_code})",
                    {"statusCode": create.status_code, "responseBody": _truncate(create.text)},
                )
            created_id = str(create.json().get("computeEnvId") or "") or None
            if not created_id:
                return ProbeResult(
                    name,
                    "degraded",
                    int((time.perf_counter() - start) * 1000),
                    "Seqera did not return a computeEnvId for the health-check env",
                    None,
                )

            # 3. Poll until the env validates (or the time budget runs out).
            status, message, detail = await _poll_compute_env_state(
                client, api_url, created_id, params, headers
            )
    except httpx.TimeoutException:
        status, message, detail = (
            "unhealthy",
            f"Tower Agent probe timed out after {int(PROBE_TIMEOUT_SECONDS)}s",
            {"error": "timeout"},
        )
    except httpx.HTTPError as exc:
        status, message, detail = (
            "unhealthy",
            f"Tower Agent probe failed: {exc}",
            {"error": str(exc)},
        )
    finally:
        # 4. Always clean up the throwaway env.
        if created_id:
            await _delete_compute_env(api_url, headers, params, created_id)

    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeResult(name, status, latency_ms, message, detail)


async def _collect_system_status() -> SystemStatus:
    """Run the probes concurrently and aggregate into an overall status.

    The Tower Agent probe is opt-in (ENABLE_AGENT_HEALTHCHECK) because it mutates
    Seqera state (creates and deletes a throwaway compute env).
    """
    probes = [_probe_seqera_api(), _probe_compute_env()]
    if _agent_probe_enabled():
        probes.append(_probe_tower_agent())

    components = list(await asyncio.gather(*probes))
    overall = _worst([c.status for c in components])
    return SystemStatus(
        overall_status=overall,
        checked_at=datetime.now(UTC),
        components=components,
    )


async def _refresh_cache() -> SystemStatus:
    """Run the probes and update both the TTL cache and the stale fallback."""
    global _last_status
    status = await _collect_system_status()
    _status_cache[_CACHE_KEY] = status
    _last_status = status
    return status


def _spawn_background_refresh() -> None:
    """Kick off a single background refresh, discarding its result.

    Guarded by ``_refresh_task`` so overlapping stale reads don't launch a
    stampede of refreshes. Exceptions are logged, never propagated — a failed
    background refresh just means we keep serving the last good status.
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return

    async def _run() -> None:
        try:
            async with _get_lock():
                # Another waiter may have refreshed while we queued on the lock.
                if _status_cache.get(_CACHE_KEY) is not None:
                    return
                await _refresh_cache()
        except Exception:
            logger.exception("Background health refresh failed; serving last known status")

    _refresh_task = asyncio.ensure_future(_run())


async def get_system_status(*, force_refresh: bool = False) -> SystemStatus:
    """Return the system status, keeping callers off the slow probe path.

    Fresh cache hit -> returned immediately. Expired cache but a prior result
    exists -> that (slightly stale) result is returned immediately and a refresh
    is kicked off in the background (stale-while-revalidate), so the caller never
    waits ~2s on the probes. Only a genuine cold start (or ``force_refresh``, used
    by the admin "refresh now" action) blocks on a live probe run. An
    ``asyncio.Lock`` collapses a cold-start stampede into a single probe run.
    """
    if not force_refresh:
        cached: SystemStatus | None = _status_cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
        if _last_status is not None:
            _spawn_background_refresh()
            return _last_status

    async with _get_lock():
        if not force_refresh:
            cached = _status_cache.get(_CACHE_KEY)
            if cached is not None:
                return cached
        return await _refresh_cache()


def _cloudwatch_log_group_url() -> str | None:
    """Build a console link to the backend log group, if configured."""
    log_group = os.getenv("SBP_BACKEND_LOG_GROUP", "").strip()
    if not log_group:
        return None
    region = os.getenv("AWS_REGION", "ap-southeast-2").strip() or "ap-southeast-2"
    # CloudWatch console encodes the log-group path with a double-encoding scheme.
    encoded = quote(quote(log_group, safe=""), safe="")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:log-groups/log-group/{encoded}"
    )


# User-facing notice shown on the job details page whenever any monitored
# component is not healthy. Intentionally generic — the portal does not surface
# which component is affected, only that data may be stale / submissions slow.
DEGRADED_USER_MESSAGE = (
    "Some workflow services are currently unavailable. Job status and logs may "
    "not be up to date, and new submissions may take longer than usual."
)


def to_components_health_dict(status: SystemStatus) -> dict[str, Any]:
    """Collapse the per-component status into a single user-facing summary.

    The job details page only needs to know whether *anything* is degraded so it
    can warn the user that job status / logs may be stale; it does not surface
    which component is affected. ``message`` is null while everything is healthy.
    """
    return {
        "overallStatus": status.overall_status,
        "checkedAt": status.checked_at,
        "message": None if status.overall_status == "healthy" else DEGRADED_USER_MESSAGE,
    }


def to_public_dict(status: SystemStatus) -> dict[str, Any]:
    """Coarse projection: status + short message only (no raw detail)."""
    return {
        "overallStatus": status.overall_status,
        "checkedAt": status.checked_at,
        "components": [
            {
                "name": c.name,
                "status": c.status,
                "latencyMs": c.latency_ms,
                "message": c.message,
            }
            for c in status.components
        ],
    }


def to_admin_dict(status: SystemStatus) -> dict[str, Any]:
    """Verbose projection: includes raw probe detail and a CloudWatch link."""
    return {
        "overallStatus": status.overall_status,
        "checkedAt": status.checked_at,
        "components": [
            {
                "name": c.name,
                "status": c.status,
                "latencyMs": c.latency_ms,
                "message": c.message,
                "detail": c.detail,
            }
            for c in status.components
        ],
        "cloudwatchLogGroupUrl": _cloudwatch_log_group_url(),
    }
