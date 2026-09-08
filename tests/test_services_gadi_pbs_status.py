"""Tests for reading the Gadi PBS queue status object pushed to S3."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.services.gadi_pbs_status import GadiPbsStatusError, get_pbs_queue_status
from app.services.s3 import S3ServiceError

_SAMPLE_PAYLOAD = json.dumps(
    {
        "generatedAt": "2026-06-01T03:00:00Z",
        "qstat": {
            "timestamp": 1735689600,
            "Queue": {
                "normal": {
                    "queue_type": "Execution",
                    "total_jobs": 5,
                    "state_count": "Transit:0 Queued:3 Held:0 Waiting:0 Running:2 Exiting:0 Begun:0",
                    "enabled": "True",
                    "started": "True",
                },
                "express": {
                    "queue_type": "Execution",
                    "total_jobs": 0,
                    "state_count": "Transit:0 Queued:0 Held:0 Waiting:0 Running:0 Exiting:0 Begun:0",
                    "enabled": "False",
                    "started": "True",
                },
            },
        },
    }
)


@pytest.mark.asyncio
async def test_get_pbs_queue_status_parses_pushed_snapshot():
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        return_value=_SAMPLE_PAYLOAD,
    ) as read_file:
        snapshot = await get_pbs_queue_status()

    assert snapshot.generated_at == datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC)
    by_name = {q.name: q for q in snapshot.queues}
    assert set(by_name) == {"normal", "express"}

    normal = by_name["normal"]
    assert normal.queue_type == "Execution"
    assert normal.total_jobs == 5
    assert normal.queued == 3
    assert normal.running == 2
    assert normal.held == 0
    assert normal.enabled is True
    assert normal.started is True

    assert by_name["express"].enabled is False
    read_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_pbs_queue_status_raises_when_object_missing():
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        side_effect=S3ServiceError("NoSuchKey"),
    ):
        with pytest.raises(GadiPbsStatusError, match="Could not read"):
            await get_pbs_queue_status()


@pytest.mark.asyncio
async def test_get_pbs_queue_status_raises_on_malformed_json():
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        return_value="not json",
    ):
        with pytest.raises(GadiPbsStatusError, match="Could not parse"):
            await get_pbs_queue_status()


@pytest.mark.asyncio
async def test_get_pbs_queue_status_raises_when_generated_at_missing():
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        return_value=json.dumps({"qstat": {"Queue": {}}}),
    ):
        with pytest.raises(GadiPbsStatusError, match="Could not parse"):
            await get_pbs_queue_status()


@pytest.mark.asyncio
async def test_get_pbs_queue_status_raises_when_qstat_missing():
    """A payload missing the 'qstat' object entirely must raise, not silently
    report zero queues - that would read as "nothing running anywhere" instead
    of "the push script wrote a malformed/incomplete file"."""
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        return_value=json.dumps({"generatedAt": "2026-06-01T03:00:00Z"}),
    ):
        with pytest.raises(GadiPbsStatusError, match="Could not parse"):
            await get_pbs_queue_status()


@pytest.mark.asyncio
async def test_get_pbs_queue_status_raises_when_queue_key_missing():
    """Same as above, but for qstat.Queue specifically missing/malformed."""
    with patch(
        "app.services.gadi_pbs_status.read_s3_file",
        new_callable=AsyncMock,
        return_value=json.dumps({"generatedAt": "2026-06-01T03:00:00Z", "qstat": {}}),
    ):
        with pytest.raises(GadiPbsStatusError, match="Could not parse"):
            await get_pbs_queue_status()
