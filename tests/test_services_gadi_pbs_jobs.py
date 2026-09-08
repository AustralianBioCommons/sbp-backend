"""Tests for reading sbp_service's Gadi PBS jobs object pushed to S3."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.services.gadi_pbs_jobs import GadiPbsJobsError, get_pbs_jobs
from app.services.s3 import S3ServiceError

_SAMPLE_PAYLOAD = json.dumps(
    {
        "generatedAt": "2026-06-01T03:00:00Z",
        "qstatJobs": {
            "timestamp": 1735689600,
            "Jobs": {
                "12345.gadi-pbs": {
                    "Job_Name": "nf-TASK_1",
                    "Job_Owner": "sbp_service@gadi-pbs",
                    "job_state": "R",
                    "queue": "normal",
                    "Account_Name": "yz52",
                    "qtime": "Mon Jun  1 02:55:00 2026",
                    "stime": "Mon Jun  1 03:00:00 2026",
                },
                "12346.gadi-pbs": {
                    "Job_Name": "nf-TASK_2",
                    "Job_Owner": "sbp_service@gadi-pbs",
                    "job_state": "Q",
                    "queue": "workflow",
                    "Account_Name": "yz52",
                    "qtime": "Mon Jun  1 02:59:00 2026",
                },
            },
        },
        "qstatQueues": {
            "Queue": {
                "normal": {
                    "state_count": "Transit:0 Queued:20 Held:0 Waiting:0 Running:100 Exiting:0",
                },
                "workflow": {
                    "state_count": "Transit:0 Queued:5 Held:0 Waiting:0 Running:1 Exiting:0",
                },
                "express": {
                    "state_count": "Transit:0 Queued:8 Held:0 Waiting:0 Running:30 Exiting:0",
                },
            }
        },
    }
)


@pytest.mark.asyncio
async def test_get_pbs_jobs_parses_pushed_snapshot():
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        return_value=_SAMPLE_PAYLOAD,
    ) as read_file:
        snapshot = await get_pbs_jobs()

    assert snapshot.generated_at == datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC)
    by_id = {j.job_id: j for j in snapshot.jobs}
    assert set(by_id) == {"12345.gadi-pbs", "12346.gadi-pbs"}

    running = by_id["12345.gadi-pbs"]
    assert running.job_name == "nf-TASK_1"
    assert running.state == "R"
    assert running.state_label == "Running"
    assert running.queue == "normal"
    assert running.account == "yz52"
    assert running.submitted_at == datetime(2026, 6, 1, 2, 55, 0, tzinfo=UTC)
    assert running.started_at == datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC)

    queued = by_id["12346.gadi-pbs"]
    assert queued.state == "Q"
    assert queued.state_label == "Queued"
    assert queued.started_at is None
    read_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_pbs_jobs_computes_queue_totals_against_own_jobs():
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        return_value=_SAMPLE_PAYLOAD,
    ):
        snapshot = await get_pbs_jobs()

    by_name = {q.name: q for q in snapshot.queue_totals}
    # "express" has Gadi-wide activity but zero sbp_service jobs, so it must
    # be excluded - only queues we actually have jobs in are worth showing.
    assert set(by_name) == {"normal", "workflow"}

    normal = by_name["normal"]
    assert normal.mine_running == 1  # our one R job is in "normal"
    assert normal.total_running == 100
    assert normal.mine_queued == 0
    assert normal.total_queued == 20

    workflow = by_name["workflow"]
    assert workflow.mine_queued == 1  # our one Q job is in "workflow"
    assert workflow.total_queued == 5
    assert workflow.mine_running == 0
    assert workflow.total_running == 1


@pytest.mark.asyncio
async def test_get_pbs_jobs_parses_real_gadi_queue_output():
    """Regression test against a real (trimmed) `qstat -Q -f -F json` capture
    from Gadi - PBS Pro splits each submission target into a "Route" queue
    (e.g. "normal") and a separate "Execution" queue ("normal-exec") with its
    own counts, state_count values have a trailing space, and other fields
    (resources_assigned, max_run, ...) are present and must be ignored rather
    than tripping up parsing."""
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {
                "Jobs": {
                    "111.gadi-pbs": {
                        "Job_Name": "nf-TASK",
                        "job_state": "R",
                        "queue": "normal-exec",
                        "Account_Name": "yz52",
                    }
                }
            },
            "qstatQueues": {
                "Queue": {
                    "normal": {
                        "queue_type": "Route",
                        "total_jobs": 327,
                        "state_count": "Transit:0 Queued:172 Held:33 Waiting:122 Running:0 "
                        "Exiting:0 Begun:0 ",
                        "route_destinations": "normal-exec",
                        "enabled": "True",
                        "started": "True",
                    },
                    "normal-exec": {
                        "queue_type": "Execution",
                        "total_jobs": 1940,
                        "state_count": "Transit:0 Queued:91 Held:676 Waiting:0 Running:1145 "
                        "Exiting:1 Begun:27 ",
                        "from_route_only": "True",
                        "resources_assigned": {
                            "mem": "125315317760kb",
                            "ncpus": 36077,
                        },
                        "enabled": "True",
                        "started": "True",
                    },
                }
            },
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        snapshot = await get_pbs_jobs()

    assert len(snapshot.jobs) == 1
    assert snapshot.jobs[0].queue == "normal-exec"

    by_name = {q.name: q for q in snapshot.queue_totals}
    # "normal" (the Route queue) has zero sbp_service jobs directly in it -
    # our one job is in "normal-exec" (the Execution queue) - so only
    # "normal-exec" should survive the relevance filter.
    assert set(by_name) == {"normal-exec"}
    assert by_name["normal-exec"].mine_running == 1
    assert by_name["normal-exec"].total_running == 1145
    assert by_name["normal-exec"].total_queued == 91


@pytest.mark.asyncio
async def test_get_pbs_jobs_queue_totals_empty_when_qstat_queues_missing():
    """Missing/malformed queue-totals section must not affect the primary
    jobs list - it's secondary context, not required."""
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {"Jobs": {"1.gadi-pbs": {"job_state": "R", "queue": "normal"}}},
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        snapshot = await get_pbs_jobs()

    assert len(snapshot.jobs) == 1
    assert snapshot.queue_totals == []


@pytest.mark.asyncio
async def test_get_pbs_jobs_maps_unknown_state_to_raw_code():
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {"Jobs": {"1.gadi-pbs": {"job_state": "X"}}},
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        snapshot = await get_pbs_jobs()

    assert snapshot.jobs[0].state == "X"
    assert snapshot.jobs[0].state_label == "X"


@pytest.mark.asyncio
async def test_get_pbs_jobs_tolerates_unparseable_timestamps():
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {
                "Jobs": {"1.gadi-pbs": {"job_state": "Q", "qtime": "not-a-real-timestamp"}}
            },
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        snapshot = await get_pbs_jobs()

    assert snapshot.jobs[0].submitted_at is None


@pytest.mark.asyncio
async def test_get_pbs_jobs_raises_when_object_missing():
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        side_effect=S3ServiceError("NoSuchKey"),
    ):
        with pytest.raises(GadiPbsJobsError, match="Could not read"):
            await get_pbs_jobs()


@pytest.mark.asyncio
async def test_get_pbs_jobs_raises_on_malformed_json():
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        return_value="not json",
    ):
        with pytest.raises(GadiPbsJobsError, match="Could not parse"):
            await get_pbs_jobs()


@pytest.mark.asyncio
async def test_get_pbs_jobs_raises_when_generated_at_missing():
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        return_value=json.dumps({"qstatJobs": {"Jobs": {}}}),
    ):
        with pytest.raises(GadiPbsJobsError, match="Could not parse"):
            await get_pbs_jobs()


@pytest.mark.asyncio
async def test_get_pbs_jobs_raises_when_qstat_jobs_missing():
    """A payload missing the 'qstatJobs' object entirely must raise, not
    silently report zero jobs - that would read as "nothing running" instead
    of "the push script wrote a malformed/incomplete file"."""
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file",
        new_callable=AsyncMock,
        return_value=json.dumps({"generatedAt": "2026-06-01T03:00:00Z"}),
    ):
        with pytest.raises(GadiPbsJobsError, match="Could not parse"):
            await get_pbs_jobs()


@pytest.mark.asyncio
async def test_get_pbs_jobs_returns_empty_list_when_jobs_key_absent():
    """Confirmed against real Gadi output: `qstat -u <user> -f -F json` omits
    the "Jobs" key entirely when zero jobs match - e.g.
    {"timestamp":..., "pbs_version":..., "pbs_server":...} with no "Jobs" key
    at all. That's a legitimate "nothing running right now" state, not a
    malformed push, so it must return an empty list rather than raising."""
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {
                "timestamp": 1788909619,
                "pbs_version": "2024.1.2.20241017100211",
                "pbs_server": "gadi-pbs-01",
            },
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        snapshot = await get_pbs_jobs()

    assert snapshot.jobs == []


@pytest.mark.asyncio
async def test_get_pbs_jobs_raises_when_jobs_value_is_malformed():
    """Unlike an absent "Jobs" key (a legitimate empty state), a present but
    non-dict "Jobs" value means the push script wrote something corrupt."""
    payload = json.dumps(
        {
            "generatedAt": "2026-06-01T03:00:00Z",
            "qstatJobs": {"Jobs": "not-a-dict"},
        }
    )
    with patch(
        "app.services.gadi_pbs_jobs.read_s3_file", new_callable=AsyncMock, return_value=payload
    ):
        with pytest.raises(GadiPbsJobsError, match="Could not parse"):
            await get_pbs_jobs()
