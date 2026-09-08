"""Gadi-wide PBS queue status, pushed from Gadi rather than pulled by this backend.

This backend has no direct connection to Gadi (no SSH/qstat access). Instead, a
script running on Gadi under the yz52_workflow service account (maintained
outside this repo) periodically runs `qstat -Q -f -F json` and writes the
result to a local file; a scheduler job then transfers it into S3 via Globus
(see services/globus_transfer.sync_gadi_pbs_queue_status) - the same S3 bucket
this backend already reads workflow outputs from (see services/s3.py). This
module just reads and parses that object.
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


class GadiPbsStatusError(RuntimeError):
    """Raised when the pushed status object is missing, unreadable, or malformed."""


@dataclass
class PbsQueueStatus:
    """One PBS queue's job counts, as reported by `qstat -Q -f` on Gadi."""

    name: str
    queue_type: str | None
    enabled: bool
    started: bool
    total_jobs: int
    queued: int
    running: int
    held: int


@dataclass
class GadiPbsStatusSnapshot:
    """The pushed status object: when it was generated, plus per-queue counts."""

    generated_at: datetime
    queues: list[PbsQueueStatus]


def _parse_state_count(raw: str) -> dict[str, int]:
    """Parse PBS's `state_count` attribute, e.g. "Transit:0 Queued:12 Running:3".

    This one field stays a space-separated "Key:Value" string even in PBS's own
    JSON output, so it needs this small amount of splitting.
    """
    counts: dict[str, int] = {}
    for part in raw.split():
        key, _, value = part.partition(":")
        if key and value.lstrip("-").isdigit():
            counts[key] = int(value)
    return counts


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _parse_queues(queues_raw: dict[str, Any]) -> list[PbsQueueStatus]:
    result = []
    for name, attrs in queues_raw.items():
        if not isinstance(attrs, dict):
            continue
        state_count = _parse_state_count(str(attrs.get("state_count", "")))
        result.append(
            PbsQueueStatus(
                name=name,
                queue_type=attrs.get("queue_type"),
                enabled=_parse_bool(attrs.get("enabled", "False")),
                started=_parse_bool(attrs.get("started", "False")),
                total_jobs=int(attrs.get("total_jobs", 0) or 0),
                queued=state_count.get("Queued", 0),
                running=state_count.get("Running", 0),
                held=state_count.get("Held", 0),
            )
        )
    return result


def _parse_snapshot(raw_json: str) -> GadiPbsStatusSnapshot:
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object at the top level")

    generated_at_raw = payload.get("generatedAt")
    if not generated_at_raw:
        raise ValueError("missing 'generatedAt'")
    generated_at = datetime.fromisoformat(str(generated_at_raw).replace("Z", "+00:00"))
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)

    qstat = payload.get("qstat", {})
    queues_raw = qstat.get("Queue", {}) if isinstance(qstat, dict) else {}
    return GadiPbsStatusSnapshot(generated_at=generated_at, queues=_parse_queues(queues_raw))


async def get_pbs_queue_status(settings: Settings | None = None) -> GadiPbsStatusSnapshot:
    """Read and parse the queue-status object pushed from Gadi.

    Raises GadiPbsStatusError if the object is missing/unreadable (e.g. the
    Gadi-side push script has stopped running) or malformed.
    """
    settings = settings or get_settings()
    key = settings.seqera.gadi_pbs_queue_status_s3_key
    try:
        raw_json = await read_s3_file(key, settings=settings)
    except Exception as exc:
        raise GadiPbsStatusError(
            f"Could not read Gadi PBS queue status from s3://{key}: {exc}"
        ) from exc

    try:
        return _parse_snapshot(raw_json)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise GadiPbsStatusError(f"Could not parse Gadi PBS queue status object: {exc}") from exc
