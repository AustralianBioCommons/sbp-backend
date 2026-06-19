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

import logging
import os
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
    HealthStatus,
)
from .seqera_errors import SeqeraConfigurationError

logger = logging.getLogger(__name__)

# Probe network budget. Kept short so a hung Seqera call cannot stall the
# dashboard / submission pre-flight check.
PROBE_TIMEOUT_SECONDS = 5.0

# Cache the whole computed status. 30s TTL keeps repeated polls (admin every 30s,
# portal every 60s) off the Seqera API while staying fresh enough to be useful.
_CACHE_TTL_SECONDS = float(os.getenv("HEALTH_CACHE_TTL_SECONDS", "30"))
_CACHE_KEY = "system_status"
_status_cache: TTLCache[str, SystemStatus] = TTLCache(maxsize=1, ttl=_CACHE_TTL_SECONDS)

# Lazily created so the module imports cleanly outside a running event loop.
_cache_lock: Any = None

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
        import asyncio

        _cache_lock = asyncio.Lock()
    return _cache_lock


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
    """Probe Seqera Platform reachability via the lightweight service-info endpoint.

    ``service-info`` is the canonical "I'm alive" endpoint and is cheaper than an
    authenticated call; we still send the access token so the probe also confirms
    our credentials are accepted. Non-2xx or timeout -> unhealthy, which lets us
    distinguish a Seqera-side outage from a compute-env problem.
    """
    name = COMPONENT_SEQERA_API
    try:
        api_url = _required_env("SEQERA_API_URL").rstrip("/")
        headers = _seqera_headers()
    except SeqeraConfigurationError as exc:
        return ProbeResult(name, "unhealthy", None, str(exc), {"error": str(exc)})

    url = f"{api_url}/service-info"
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
        # If we cannot read the compute env, we cannot vouch for the agent.
        return ProbeResult(
            name,
            "unhealthy",
            latency_ms,
            f"Could not read compute environment (HTTP {response.status_code})",
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
    if status == "healthy":
        message = None
    else:
        message = f"Compute environment state: {state or 'UNKNOWN'}"
        if isinstance(env_message, str) and env_message.strip():
            message = f"{message} ({env_message.strip()})"

    return ProbeResult(name, status, latency_ms, message, {"computeEnv": compute_env})


async def _collect_system_status() -> SystemStatus:
    """Run both probes and aggregate into an overall status."""
    import asyncio

    api_result, compute_result = await asyncio.gather(
        _probe_seqera_api(),
        _probe_compute_env(),
    )
    components = [api_result, compute_result]
    overall = _worst([c.status for c in components])
    return SystemStatus(
        overall_status=overall,
        checked_at=datetime.now(UTC),
        components=components,
    )


async def get_system_status(*, force_refresh: bool = False) -> SystemStatus:
    """Return the (cached) system status, refreshing on cache miss.

    Uses an ``asyncio.Lock`` so that concurrent callers on a cold cache trigger a
    single set of probes rather than a stampede.
    """
    if not force_refresh:
        cached: SystemStatus | None = _status_cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

    async with _get_lock():
        if not force_refresh:
            cached = _status_cache.get(_CACHE_KEY)
            if cached is not None:
                return cached
        status = await _collect_system_status()
        _status_cache[_CACHE_KEY] = status
        return status


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
