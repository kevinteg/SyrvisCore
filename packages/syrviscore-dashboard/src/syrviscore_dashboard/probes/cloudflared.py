"""Cloudflared probe — makes the tunnel first-class.

The tunnel is only "configured" if a ``CLOUDFLARE_TUNNEL_TOKEN`` is set; otherwise
we report ``NOT_CONFIGURED`` (never DOWN). When configured we hit the metrics
``/ready`` endpoint (enabled via ``TUNNEL_METRICS`` on the cloudflared container):
its JSON ``readyConnections`` tells us whether the tunnel actually has live edge
connections, not just whether the container is up.
"""

import httpx

from .base import ProbeResult, Status
from ._config import component_enabled


async def probe_cloudflared(settings, http: httpx.AsyncClient) -> ProbeResult:
    if not component_enabled("cloudflared"):
        return ProbeResult(
            "cloudflared", Status.NOT_CONFIGURED, "no CLOUDFLARE_TUNNEL_TOKEN configured"
        )

    base = settings.cloudflared_url.rstrip("/")
    try:
        resp = await http.get(base + "/ready")
    except httpx.HTTPError as exc:
        # Token is set but metrics unreachable — likely TUNNEL_METRICS not applied
        # yet (needs a compose recreate). Degraded, not down.
        return ProbeResult(
            "cloudflared",
            Status.DEGRADED,
            "tunnel configured but metrics endpoint unreachable "
            "(TUNNEL_METRICS may need a `syrvis start`): {}".format(exc),
        )

    extra = {}
    ready_connections = None
    try:
        data = resp.json()
        ready_connections = data.get("readyConnections")
        extra["readyConnections"] = ready_connections
        extra["connectorId"] = data.get("connectorId")
    except ValueError:
        pass

    if resp.status_code == 200 and (ready_connections or 0) > 0:
        return ProbeResult(
            "cloudflared", Status.OK, "{} edge connection(s)".format(ready_connections), extra=extra
        )
    return ProbeResult(
        "cloudflared", Status.DEGRADED, "tunnel up but no ready edge connections", extra=extra
    )
