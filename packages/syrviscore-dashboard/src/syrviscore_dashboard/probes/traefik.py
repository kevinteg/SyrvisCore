"""Traefik probe — the real "Traefik test".

Traefik's API/dashboard runs on :8080 inside the ``proxy`` network (the generated
static config sets ``api.dashboard`` + ``api.insecure``). We hit ``/ping`` (the
canonical liveness endpoint) and ``/api/overview`` + ``/api/http/routers`` for
routing detail.
"""

import httpx

from .base import ProbeResult, Status


async def probe_traefik(settings, http: httpx.AsyncClient) -> ProbeResult:
    base = settings.traefik_url.rstrip("/")
    try:
        ping = await http.get(base + "/ping")
    except httpx.HTTPError as exc:
        return ProbeResult("traefik", Status.DOWN, "unreachable: {}".format(exc))

    if ping.status_code != 200:
        return ProbeResult("traefik", Status.DOWN, "ping returned {}".format(ping.status_code))

    extra = {}
    status = Status.OK
    detail = "ping ok"
    try:
        overview = await http.get(base + "/api/overview")
        if overview.status_code == 200:
            data = overview.json()
            http_info = data.get("http", {})
            extra["routers"] = http_info.get("routers", {})
            extra["services"] = http_info.get("services", {})
            extra["middlewares"] = http_info.get("middlewares", {})
            extra["features"] = data.get("features", {})
        else:
            status = Status.DEGRADED
            detail = "ping ok, /api/overview returned {}".format(overview.status_code)
    except httpx.HTTPError as exc:
        status = Status.DEGRADED
        detail = "ping ok, api error: {}".format(exc)

    try:
        routers = await http.get(base + "/api/http/routers")
        if routers.status_code == 200:
            extra["router_names"] = sorted(
                r.get("name", "") for r in routers.json() if isinstance(r, dict)
            )
    except httpx.HTTPError:
        pass  # router list is a nice-to-have

    return ProbeResult("traefik", status, detail, extra=extra)
