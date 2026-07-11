"""Container logs — one-shot text or a bounded live SSE stream."""

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from .. import docker_util
from ..sse import LOG_STREAM_MAX_LINES, SSE_HEADERS
from ._errors import as_http

router = APIRouter(prefix="/api", tags=["live"])


@router.get("/logs/{service}")
async def logs(
    service: str,
    request: Request,
    tail: int = Query(200, ge=1, le=5000),
    stream: bool = Query(False, description="follow via SSE instead of a one-shot snapshot"),
):
    try:
        container = await asyncio.to_thread(docker_util.get_managed_container, service)
    except Exception as exc:  # noqa: BLE001 - mapped to a clean HTTP status
        raise as_http(exc)

    if not stream:
        text = await asyncio.to_thread(
            lambda: container.logs(tail=tail, timestamps=True).decode(errors="replace")
        )
        return PlainTextResponse(text)

    async def generator():
        iterator = await asyncio.to_thread(
            lambda: iter(container.logs(stream=True, follow=True, tail=tail, timestamps=True))
        )
        count = 0
        while count < LOG_STREAM_MAX_LINES:
            if await request.is_disconnected():
                break
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            line = chunk.decode(errors="replace").rstrip("\n")
            if line:
                yield {"event": "log", "data": line}
            count += 1

    return EventSourceResponse(generator(), headers=SSE_HEADERS)
