"""Runtime health probes for the two components workflow submission depends on.

Two probes, both reached through the Seqera Platform API:

1. ``seqera_api`` — can we talk to the Seqera Platform at all?
2. ``seqera_compute_env`` — is the Gadi-backed compute environment AVAILABLE?
   Seqera reports the Tower Agent connection state via the compute-env ``status``
   field, which is the closest proxy we have for Gadi-side health.

Results are cached in the database for a short TTL so polling (admin dashboard,
portal banner, scheduler) stays cheap across processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db.models.system_status import SystemStatusCache, SystemStatusIncident
from ..schemas.health import (
    COMPONENT_COMPUTE_ENV,
    COMPONENT_SEQERA_API,
    COMPONENT_TOWER_AGENT,
    HealthStatus,
    ProbeResult,
    SystemStatus,
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

# Cache the whole computed status in the database. 30s TTL keeps repeated polls
# (admin every 30s, portal every 60s, scheduler checks) off the Seqera API while
# staying fresh enough to be useful across processes.
_CACHE_TTL_SECONDS = float(os.getenv("HEALTH_CACHE_TTL_SECONDS", "30"))
_CACHE_KEY = "system_status"
_CACHE_LOCK_ID = hash(_CACHE_KEY)

# Seqera compute-env state -> our normalized health bucket.
_COMPUTE_ENV_STATE_MAP: dict[str, HealthStatus] = {
    "AVAILABLE": "healthy",
    "CREATING": "degraded",
    "ERRORED": "unhealthy",
    "OFFLINE": "unhealthy",
    "INVALID": "unhealthy",
}


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


async def probe_seqera_api() -> ProbeResult:
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
        return ProbeResult(
            name=name, status="unhealthy", message=str(exc), detail={"error": str(exc)}
        )

    url = f"{api_url}/user-info"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=f"Seqera API did not respond within {int(PROBE_TIMEOUT_SECONDS)}s",
            detail={"error": "timeout", "url": url},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=f"Seqera API unreachable: {exc}",
            detail={"error": str(exc), "url": url},
        )

    if response.status_code in (401, 403):
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=f"Seqera rejected the access token (HTTP {response.status_code}); "
            "check SEQERA_ACCESS_TOKEN",
            detail={
                "statusCode": response.status_code,
                "responseBody": _truncate(response.text),
                "url": url,
            },
        )

    if response.is_error:
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=f"Seqera API returned HTTP {response.status_code}",
            detail={
                "statusCode": response.status_code,
                "responseBody": _truncate(response.text),
                "url": url,
            },
        )

    return ProbeResult(name=name, status="healthy", latency_ms=latency_ms)


async def probe_compute_env() -> ProbeResult:
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
        return ProbeResult(
            name=name, status="unhealthy", message=str(exc), detail={"error": str(exc)}
        )

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
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=(
                f"Compute environment check did not respond within "
                f"{int(PROBE_TIMEOUT_SECONDS)}s"
            ),
            detail={"error": "timeout", "url": url},
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=f"Compute environment unreachable: {exc}",
            detail={"error": str(exc), "url": url},
        )

    if response.is_error:
        # If we cannot read the compute env, we cannot vouch for the agent. A
        # 403/404 here typically means a wrong WORK_SPACE / COMPUTE_ID or a token
        # without access to them, so call that out explicitly.
        error_message = f"Could not read compute environment (HTTP {response.status_code})"
        if response.status_code in (403, 404):
            error_message = f"{error_message}; check COMPUTE_ID, WORK_SPACE, and the access token"
        return ProbeResult(
            name=name,
            status="unhealthy",
            latency_ms=latency_ms,
            message=error_message,
            detail={
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

    return ProbeResult(
        name=name,
        status=status,
        latency_ms=latency_ms,
        message=message,
        detail={"computeEnv": compute_env},
    )


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


async def probe_tower_agent() -> ProbeResult:
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
        return ProbeResult(
            name=name, status="unhealthy", message=str(exc), detail={"error": str(exc)}
        )

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
                    name=name,
                    status="unhealthy",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    message=f"Could not read source compute env to clone (HTTP {src.status_code})",
                    detail={"statusCode": src.status_code, "responseBody": _truncate(src.text)},
                )
            src_env = src.json().get("computeEnv", {})
            platform = src_env.get("platform")
            config = src_env.get("config")
            credentials_id = src_env.get("credentialsId")
            if not (platform and config and credentials_id):
                return ProbeResult(
                    name=name,
                    status="degraded",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    message="Source compute env is missing platform/config/credentialsId to clone",
                    detail={"platform": platform, "hasConfig": bool(config)},
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
                    name=name,
                    status="unhealthy",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    message=(
                        f"Tower Agent health-check env creation was rejected "
                        f"(HTTP {create.status_code})"
                    ),
                    detail={
                        "statusCode": create.status_code,
                        "responseBody": _truncate(create.text),
                    },
                )
            created_id = str(create.json().get("computeEnvId") or "") or None
            if not created_id:
                return ProbeResult(
                    name=name,
                    status="degraded",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    message="Seqera did not return a computeEnvId for the health-check env",
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
    return ProbeResult(
        name=name,
        status=status,
        latency_ms=latency_ms,
        message=message,
        detail=detail,
    )


async def collect_system_status() -> SystemStatus:
    """Run the probes concurrently and aggregate into an overall status.

    The Tower Agent probe is opt-in (ENABLE_AGENT_HEALTHCHECK) because it mutates
    Seqera state (creates and deletes a throwaway compute env).
    """
    probes = [probe_seqera_api(), probe_compute_env()]
    if _agent_probe_enabled():
        probes.append(probe_tower_agent())

    components = list(await asyncio.gather(*probes))
    overall = _worst([c.status for c in components])
    return SystemStatus(
        overall_status=overall,
        checked_at=datetime.now(UTC),
        components=components,
    )


def _is_postgresql(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql"


def _try_acquire_cache_refresh_lock(db: Session) -> bool:
    """
    Try to get a lock on the shared DB cache - if we can't get the lock,
    assume another process is already refreshing the cache.
    """
    if not _is_postgresql(db):
        return True
    return bool(db.execute(select(func.pg_try_advisory_xact_lock(_CACHE_LOCK_ID))).scalar_one())


def _open_incident(db: Session, status: SystemStatus, component: ProbeResult) -> None:
    db.add(
        SystemStatusIncident(
            component=component.name,
            status=component.status,
            started_at=status.checked_at,
            message=component.message,
        )
    )


def _update_incidents(db: Session, status: SystemStatus) -> None:
    """Open/close/split incident rows so only downtime periods are stored.

    Healthy checks are never recorded. A component's most recent incident with
    ``ended_at IS NULL`` (if any) is its currently-open outage:

    - Recovers to healthy -> close it.
    - Still down at the same severity -> leave it open (refresh the message).
    - Still down but severity changed (e.g. degraded -> unhealthy) -> close it
      and open a new one, so each row represents a single severity level.
    - No open incident and still down -> open a new one.
    """
    for component in status.components:
        open_incident = db.execute(
            select(SystemStatusIncident)
            .where(SystemStatusIncident.component == component.name)
            .where(SystemStatusIncident.ended_at.is_(None))
            .order_by(SystemStatusIncident.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if component.status == "healthy":
            if open_incident is not None:
                open_incident.ended_at = status.checked_at
                db.add(open_incident)
            continue

        if open_incident is None:
            _open_incident(db, status, component)
        elif open_incident.status != component.status:
            open_incident.ended_at = status.checked_at
            db.add(open_incident)
            _open_incident(db, status, component)
        elif component.message != open_incident.message:
            open_incident.message = component.message
            db.add(open_incident)


async def refresh_db_cache(db: Session) -> SystemStatus:
    """Run probes, replace the single shared DB cache row, and log incidents."""
    status = await collect_system_status()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=_CACHE_TTL_SECONDS)
    payload = status.model_dump(mode="json")

    row = db.get(SystemStatusCache, _CACHE_KEY)
    if row is None:
        row = SystemStatusCache(
            key=_CACHE_KEY,
            payload=payload,
            checked_at=status.checked_at,
            expires_at=expires_at,
            updated_at=now,
        )
    else:
        row.payload = payload
        row.checked_at = status.checked_at
        row.expires_at = expires_at
        row.updated_at = now

    db.add(row)
    _update_incidents(db, status)
    db.commit()
    return status


async def get_system_status(
    db: Session | None = None,
    *,
    force_refresh: bool = False,
    allow_stale: bool = True,
) -> SystemStatus:
    """Return the system status using the shared DB cache when available.

    If ``db`` is None, this function runs the probes directly without caching.
    """
    if db is None:
        return await collect_system_status()

    now = datetime.now(UTC)
    row = db.get(SystemStatusCache, _CACHE_KEY)
    if row is not None:
        if row.is_fresh(now=now) and not force_refresh:
            return row.get_status()

        is_locked_by_other = not _try_acquire_cache_refresh_lock(db)
        if is_locked_by_other:
            if allow_stale and not force_refresh:
                return row.get_status()
            return await collect_system_status()
    else:
        is_locked_by_other = not _try_acquire_cache_refresh_lock(db)
        if is_locked_by_other:
            return await collect_system_status()

    db.expire_all()
    row = db.get(SystemStatusCache, _CACHE_KEY)
    now = datetime.now(UTC)
    if row is not None and not force_refresh and row.is_fresh(now=now):
        return row.get_status()

    try:
        return await refresh_db_cache(db)
    except Exception:
        db.rollback()
        raise


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


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def get_incidents(
    db: Session,
    *,
    since: datetime,
    until: datetime,
    component: str | None = None,
) -> list[SystemStatusIncident]:
    """Return incidents overlapping ``[since, until]``, oldest first.

    An incident overlaps the window if it started at/before ``until`` and
    either is still open (``ended_at IS NULL``) or ended at/after ``since``.
    """
    stmt = select(SystemStatusIncident).where(
        SystemStatusIncident.started_at <= until,
        or_(
            SystemStatusIncident.ended_at.is_(None),
            SystemStatusIncident.ended_at >= since,
        ),
    )
    if component:
        stmt = stmt.where(SystemStatusIncident.component == component)
    stmt = stmt.order_by(SystemStatusIncident.component, SystemStatusIncident.started_at)
    return list(db.execute(stmt).scalars())


def _clipped_downtime_seconds(
    incident: SystemStatusIncident, *, since: datetime, until: datetime
) -> float:
    """Downtime contributed by ``incident`` within ``[since, until]``.

    Incidents that started before ``since`` or are still ongoing (``ended_at``
    is None, so it counts as down through ``until``) are clipped to the window
    so a long-running outage doesn't over-count time outside the query range.
    """
    start = max(_as_utc(incident.started_at), since)
    end = min(_as_utc(incident.ended_at) if incident.ended_at is not None else until, until)
    return max((end - start).total_seconds(), 0.0)


def _summarize_downtime(
    incidents: list[SystemStatusIncident], *, since: datetime, until: datetime
) -> dict[str, Any]:
    downtime_seconds = sum(
        _clipped_downtime_seconds(incident, since=since, until=until) for incident in incidents
    )
    window_seconds = max((until - since).total_seconds(), 0.0)
    uptime_percent = (
        round(max(window_seconds - downtime_seconds, 0.0) / window_seconds * 100, 2)
        if window_seconds
        else 100.0
    )
    return {
        "incidentCount": len(incidents),
        "downtimeSeconds": round(downtime_seconds, 2),
        "uptimePercent": uptime_percent,
    }


def to_downtime_dict(
    incidents: list[SystemStatusIncident], *, since: datetime, until: datetime, hours: int
) -> dict[str, Any]:
    """Group flat incident rows by component into the admin downtime response shape."""
    by_component: dict[str, list[SystemStatusIncident]] = {}
    for incident in incidents:
        by_component.setdefault(incident.component, []).append(incident)

    components = [
        {
            "name": name,
            "summary": _summarize_downtime(rows, since=since, until=until),
            "incidents": [
                {
                    "status": row.status,
                    "startedAt": row.started_at,
                    "endedAt": row.ended_at,
                    "message": row.message,
                }
                for row in rows
            ],
        }
        for name, rows in by_component.items()
    ]
    return {"windowHours": hours, "since": since, "until": until, "components": components}


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
