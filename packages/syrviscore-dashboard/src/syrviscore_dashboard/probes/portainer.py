"""Portainer probe — GET /api/system/status (unauthenticated version/instance info).

The older /api/status spelling serves an identical payload but is deprecated: Portainer
logs a WRN on every hit and drops the route at its next major.
"""

import httpx

from .base import ProbeResult, Status


async def probe_portainer(settings, http: httpx.AsyncClient) -> ProbeResult:
    base = settings.portainer_url.rstrip("/")
    try:
        resp = await http.get(base + "/api/system/status")
    except httpx.HTTPError as exc:
        return ProbeResult("portainer", Status.DOWN, "unreachable: {}".format(exc))

    if resp.status_code != 200:
        return ProbeResult(
            "portainer", Status.DEGRADED, "/api/system/status returned {}".format(resp.status_code)
        )

    extra = {}
    try:
        data = resp.json()
        extra["version"] = data.get("Version")
        extra["instance_id"] = data.get("InstanceID")
    except ValueError:
        pass

    return ProbeResult("portainer", Status.OK, "reachable", extra=extra)
