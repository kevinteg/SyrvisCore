"""Live health stream — one SSE endpoint pushing aggregated snapshots."""

import asyncio
import json
from typing import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ..aggregator import HealthAggregator
from ..deps import get_aggregator, get_settings_dep
from ..settings import DashboardSettings
from ..sse import SSE_HEADERS

router = APIRouter(prefix="/api", tags=["live"])

# Floor on the push interval so a tiny TTL can't turn into a busy loop.
_MIN_INTERVAL_S = 0.5


async def health_event_stream(
    agg: HealthAggregator,
    settings: DashboardSettings,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict]:
    """Yield an SSE ``health`` event per interval until the client disconnects.

    Extracted from the route so it can be unit-tested without an HTTP server
    (an infinite streaming response is awkward to drive through a TestClient).
    """
    while True:
        if await is_disconnected():
            break
        snapshot = await agg.get_snapshot()
        yield {"event": "health", "data": json.dumps(snapshot)}
        await asyncio.sleep(max(settings.aggregator_ttl_s, _MIN_INTERVAL_S))


@router.get("/events")
async def events(
    request: Request,
    agg: HealthAggregator = Depends(get_aggregator),
    settings: DashboardSettings = Depends(get_settings_dep),
) -> EventSourceResponse:
    """Push the aggregated health snapshot every TTL window until the client leaves."""
    return EventSourceResponse(
        health_event_stream(agg, settings, request.is_disconnected), headers=SSE_HEADERS
    )
