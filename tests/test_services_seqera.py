"""Tests for Seqera service."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.seqera import count_active_workflows, get_queue_status
from app.services.seqera_errors import SeqeraAPIError


def _totals_by_status_handler(totals: dict[str, int]):
    """Fake /workflow endpoint: returns totalSize for whichever status:<value> was searched."""

    def _handler(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        status = search.removeprefix("status:")
        return httpx.Response(
            200, json={"workflows": [], "totalSize": totals.get(status, 0), "hasMore": False}
        )

    return _handler


@pytest.mark.asyncio
@respx.mock
async def test_count_active_workflows_sums_running_and_submitted_totals():
    respx.get(url__regex=r".*/workflow(\?.*)?$").mock(
        side_effect=_totals_by_status_handler({"RUNNING": 3, "SUBMITTED": 2})
    )

    assert await count_active_workflows() == 5


@pytest.mark.asyncio
@respx.mock
async def test_count_active_workflows_queries_each_status_with_a_minimal_page():
    route = respx.get(url__regex=r".*/workflow(\?.*)?$").mock(
        side_effect=_totals_by_status_handler({})
    )

    await count_active_workflows()

    assert route.call_count == 2
    searches = {call.request.url.params["search"] for call in route.calls}
    assert searches == {"status:RUNNING", "status:SUBMITTED"}
    assert all(call.request.url.params["max"] == "1" for call in route.calls)


@pytest.mark.asyncio
@respx.mock
async def test_count_active_workflows_no_active_runs():
    respx.get(url__regex=r".*/workflow(\?.*)?$").mock(side_effect=_totals_by_status_handler({}))

    assert await count_active_workflows() == 0


@pytest.mark.asyncio
@respx.mock
async def test_count_active_workflows_raises_on_api_error():
    respx.get(url__regex=r".*/workflow(\?.*)?$").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(SeqeraAPIError):
        await count_active_workflows()


@pytest.mark.asyncio
@respx.mock
async def test_get_queue_status_reports_active_and_available_capacity():
    respx.get(url__regex=r".*/workflow(\?.*)?$").mock(
        side_effect=_totals_by_status_handler({"RUNNING": 3, "SUBMITTED": 2})
    )

    status = await get_queue_status()

    assert status.active_workflows == 5
    assert status.max_concurrent_workflows == 25
    assert status.available_capacity == 20


@pytest.mark.asyncio
@respx.mock
async def test_get_queue_status_capacity_floors_at_zero_when_over_cap():
    respx.get(url__regex=r".*/workflow(\?.*)?$").mock(
        side_effect=_totals_by_status_handler({"RUNNING": 20, "SUBMITTED": 10})
    )

    status = await get_queue_status()

    assert status.active_workflows == 30
    assert status.available_capacity == 0
