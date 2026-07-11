"""Portainer probe — GET /api/status (unauthenticated version/instance info)."""

import httpx

from .base import ProbeResult, Status


async def probe_portainer(settings, http: httpx.AsyncClient) -> ProbeResult:
    base = settings.portainer_url.rstrip("/")
    try:
        resp = await http.get(base + "/api/status")
    except httpx.HTTPError as exc:
        return ProbeResult("portainer", Status.DOWN, "unreachable: {}".format(exc))

    if resp.status_code != 200:
        return ProbeResult(
            "portainer", Status.DEGRADED, "/api/status returned {}".format(resp.status_code)
        )

    extra = {}
    try:
        data = resp.json()
        extra["version"] = data.get("Version")
        extra["instance_id"] = data.get("InstanceID")
    except ValueError:
        pass

    return ProbeResult("portainer", Status.OK, "reachable", extra=extra)
