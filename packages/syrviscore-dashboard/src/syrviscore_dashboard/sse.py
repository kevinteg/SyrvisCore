"""Shared Server-Sent-Events helpers.

These headers stop Traefik and Cloudflare from buffering the stream, which would
otherwise defeat the point of live updates. ``sse-starlette`` adds periodic ping
comments on top to keep the connection (and the tunnel) alive.
"""

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # disable proxy buffering (nginx/traefik)
    "Connection": "keep-alive",
}

# Hard cap on lines pushed per log stream, so a chatty container can't run forever.
LOG_STREAM_MAX_LINES = 5000
