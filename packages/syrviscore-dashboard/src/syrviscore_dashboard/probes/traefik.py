"""Traefik probe — the real "Traefik test".

Traefik's API/dashboard runs on :8080 inside the ``proxy`` network. ``/ping`` is
the ideal liveness signal, but not every Traefik config enables it — so we fall
back to ``/api/overview``: if the API answers, Traefik is up even when ``/ping``
404s (report *degraded* rather than *down*).
"""

import httpx

from .base import ProbeResult, Status


async def probe_traefik(settings, http: httpx.AsyncClient) -> ProbeResult:
    base = settings.traefik_url.rstrip("/")

    ping_ok = False
    ping_code = None
    try:
        ping = await http.get(base + "/ping")
        ping_code = ping.status_code
        ping_ok = ping.status_code == 200
    except httpx.HTTPError:
        ping_ok = False

    extra = {}
    overview_ok = False
    try:
        overview = await http.get(base + "/api/overview")
        if overview.status_code == 200:
            overview_ok = True
            data = overview.json()
            http_info = data.get("http", {})
            extra["routers"] = http_info.get("routers", {})
            extra["services"] = http_info.get("services", {})
            extra["middlewares"] = http_info.get("middlewares", {})
            extra["features"] = data.get("features", {})
    except httpx.HTTPError:
        overview_ok = False

    if overview_ok:
        try:
            routers = await http.get(base + "/api/http/routers")
            if routers.status_code == 200:
                extra["router_names"] = sorted(
                    r.get("name", "") for r in routers.json() if isinstance(r, dict)
                )
        except httpx.HTTPError:
            pass  # router list is a nice-to-have

    if ping_ok:
        if overview_ok:
            return ProbeResult("traefik", Status.OK, "ping ok", extra=extra)
        return ProbeResult(
            "traefik", Status.DEGRADED, "ping ok, /api/overview unavailable", extra=extra
        )

    if overview_ok:
        # API reachable but /ping missing/non-200 — Traefik is up, just no ping route.
        code = ping_code if ping_code is not None else "no response"
        return ProbeResult(
            "traefik",
            Status.DEGRADED,
            "up (API reachable) but /ping returned {}".format(code),
            extra=extra,
        )

    # Neither /ping nor the API answered — genuinely unreachable.
    code = ping_code if ping_code is not None else "error"
    return ProbeResult("traefik", Status.DOWN, "unreachable (/ping={})".format(code))
