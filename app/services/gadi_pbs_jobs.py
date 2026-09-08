"""Gadi PBS jobs owned by the sbp_service account, pushed from Gadi rather
than pulled by this backend.

This backend has no direct connection to Gadi (no SSH/qstat access). Instead, a
script running on Gadi under the yz52_workflow service account (maintained
outside this repo) periodically runs two qstat commands and writes the
combined result to a local file; a scheduler job then transfers it into S3 via
Globus (see services/globus_transfer.sync_gadi_pbs_jobs) - the same S3 bucket
this backend already reads workflow outputs from (see services/s3.py). This
module just reads and parses that object.

Primary data - `qstat -u sbp_service -f -F json` (job-listing mode): every job
owned by sbp_service, individually. A per-queue count (`qstat -Q`) has no
per-user filter and would include every other Gadi user's jobs too, so this is
the only way to see just our own jobs.

Secondary data - `qstat -Q -f -F json` (queue-summary mode): per-queue totals
across ALL Gadi users, used only to show "ours / total" alongside the primary
job list for context (e.g. "10 of our jobs running out of 100 total in
`normal`"). Optional: a missing/malformed queue-totals section degrades to an
empty list rather than failing the whole response, since the job list above is
the primary thing this endpoint exists for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import Settings, get_settings
from .s3 import read_s3_file

logger = logging.getLogger(__name__)

# PBS's single-letter job_state codes (qstat -f), for a readable label.
_JOB_STATE_LABELS: dict[str, str] = {
    "Q": "Queued",
    "R": "Running",
    "H": "Held",
    "E": "Exiting",
    "F": "Finished",
    "S": "Suspended",
    "T": "Transiting",
    "W": "Waiting",
    "M": "Moved",
}

# PBS's own timestamp format for ctime/qtime/stime in qstat -f (unaffected by
# -F json - PBS wraps the same string, doesn't reformat it), e.g.
# "Mon Jun  1 03:00:00 2026".
_PBS_DATETIME_FORMAT = "%a %b %d %H:%M:%S %Y"


class GadiPbsJobsError(RuntimeError):
    """Raised when the pushed jobs object is missing, unreadable, or malformed."""


@dataclass
class PbsJobStatus:
    """One PBS job owned by sbp_service, as reported by `qstat -u sbp_service -f`."""

    job_id: str
    job_name: str | None
    state: str
    state_label: str
    queue: str | None
    account: str | None
    submitted_at: datetime | None
    started_at: datetime | None


@dataclass
class QueueTotal:
    """Gadi-wide totals for one queue, alongside sbp_service's own share of it."""

    name: str
    mine_queued: int
    total_queued: int
    mine_running: int
    total_running: int
    mine_held: int
    total_held: int


@dataclass
class GadiPbsJobsSnapshot:
    """The pushed object: when it was generated, sbp_service's own jobs, and
    (best-effort) per-queue totals for context."""

    generated_at: datetime
    jobs: list[PbsJobStatus]
    queue_totals: list[QueueTotal]


def _parse_pbs_datetime(raw: Any) -> datetime | None:
    """Best-effort parse of a PBS ctime/qtime/stime string.

    Lenient by design (unlike our own `generatedAt`, which we control the
    format of): PBS's date formatting isn't something this backend controls,
    so a job missing/malformed timestamp shouldn't fail the whole response -
    just that one field.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), _PBS_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_jobs(jobs_raw: dict[str, Any]) -> list[PbsJobStatus]:
    result = []
    for job_id, attrs in jobs_raw.items():
        if not isinstance(attrs, dict):
            continue
        state = str(attrs.get("job_state", "")).strip().upper()
        result.append(
            PbsJobStatus(
                job_id=job_id,
                job_name=attrs.get("Job_Name"),
                state=state,
                state_label=_JOB_STATE_LABELS.get(state, state or "Unknown"),
                queue=attrs.get("queue"),
                account=attrs.get("Account_Name"),
                submitted_at=_parse_pbs_datetime(attrs.get("qtime")),
                started_at=_parse_pbs_datetime(attrs.get("stime")),
            )
        )
    return result


def _parse_state_count(raw: str) -> dict[str, int]:
    """Parse PBS's `state_count` attribute, e.g. "Transit:0 Queued:12 Running:3".

    This one field stays a space-separated "Key:Value" string even in PBS's
    own JSON output, so it needs this small amount of splitting.
    """
    counts: dict[str, int] = {}
    for part in raw.split():
        key, _, value = part.partition(":")
        if key and value.lstrip("-").isdigit():
            counts[key] = int(value)
    return counts


def _count_mine_by_queue_and_state(jobs: list[PbsJobStatus]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for job in jobs:
        if not job.queue:
            continue
        key = (job.queue, job.state)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _parse_queue_totals(
    queues_raw: Any, mine_counts: dict[tuple[str, str], int]
) -> list[QueueTotal]:
    if not isinstance(queues_raw, dict):
        return []
    result = []
    for name, attrs in queues_raw.items():
        if not isinstance(attrs, dict):
            continue
        mine_queued = mine_counts.get((name, "Q"), 0)
        mine_running = mine_counts.get((name, "R"), 0)
        mine_held = mine_counts.get((name, "H"), 0)
        if mine_queued == 0 and mine_running == 0 and mine_held == 0:
            # Skip queues sbp_service has no jobs in at all - qstat -Q lists
            # every Gadi queue (~15), most of which are irrelevant to us; a
            # kept row still shows the full Gadi-wide total, not just ours.
            continue
        state_count = _parse_state_count(str(attrs.get("state_count", "")))
        result.append(
            QueueTotal(
                name=name,
                mine_queued=mine_queued,
                total_queued=state_count.get("Queued", 0),
                mine_running=mine_running,
                total_running=state_count.get("Running", 0),
                mine_held=mine_held,
                total_held=state_count.get("Held", 0),
            )
        )
    return result


def _parse_snapshot(raw_json: str) -> GadiPbsJobsSnapshot:
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object at the top level")

    generated_at_raw = payload.get("generatedAt")
    if not generated_at_raw:
        raise ValueError("missing 'generatedAt'")
    generated_at = datetime.fromisoformat(str(generated_at_raw).replace("Z", "+00:00"))
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)

    # Primary: sbp_service's own jobs. Strict - a missing/malformed section
    # here must raise, not silently report zero jobs (that would read as
    # "nothing running" instead of "the push script wrote a bad file").
    qstat_jobs = payload.get("qstatJobs")
    if not isinstance(qstat_jobs, dict):
        raise ValueError("missing or malformed 'qstatJobs' object")
    # PBS's own `qstat -f -F json` omits the "Jobs" key entirely when zero
    # jobs match the filter (confirmed against real Gadi output) - that's a
    # legitimate "nothing running right now" state, not a malformed push, so
    # it defaults to empty rather than raising. A non-dict value for a
    # *present* "Jobs" key is still treated as corrupt.
    jobs_raw = qstat_jobs.get("Jobs", {})
    if not isinstance(jobs_raw, dict):
        raise ValueError("malformed 'qstatJobs.Jobs' object")
    jobs = _parse_jobs(jobs_raw)

    # Secondary: Gadi-wide queue totals, for "ours / total" context only.
    # Lenient - missing/malformed here must not take down the primary jobs
    # list above, so this degrades to an empty list instead of raising.
    qstat_queues = payload.get("qstatQueues")
    queues_raw = qstat_queues.get("Queue") if isinstance(qstat_queues, dict) else None
    mine_counts = _count_mine_by_queue_and_state(jobs)
    queue_totals = _parse_queue_totals(queues_raw, mine_counts)

    return GadiPbsJobsSnapshot(generated_at=generated_at, jobs=jobs, queue_totals=queue_totals)


async def get_pbs_jobs(settings: Settings | None = None) -> GadiPbsJobsSnapshot:
    """Read and parse the sbp_service jobs object pushed from Gadi.

    Raises GadiPbsJobsError if the object is missing/unreadable (e.g. the
    Gadi-side push script has stopped running) or malformed.
    """
    settings = settings or get_settings()
    key = settings.seqera.gadi_pbs_jobs_s3_key
    try:
        raw_json = await read_s3_file(key, settings=settings)
    except Exception as exc:
        raise GadiPbsJobsError(f"Could not read Gadi PBS jobs from s3://{key}: {exc}") from exc

    try:
        return _parse_snapshot(raw_json)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise GadiPbsJobsError(f"Could not parse Gadi PBS jobs object: {exc}") from exc
