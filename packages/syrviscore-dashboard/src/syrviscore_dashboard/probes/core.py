"""Core-stack probe: container states + desired-vs-actual drift.

Reuses the ``syrviscore`` library in-process:
- ``DockerManager.get_container_status()`` for the running containers,
- ``verify.gather_core_drift(actual=...)`` for drift vs the generated compose.
The single ``get_container_status()`` call is reused for both (no double hit).
"""

import asyncio

from .base import ProbeResult, Status


def _core_sync(settings) -> ProbeResult:
    from syrviscore.docker_manager import DockerConnectionError, DockerManager

    try:
        mgr = DockerManager()
    except DockerConnectionError as exc:
        return ProbeResult("core", Status.DOWN, "docker unreachable: {}".format(exc))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult("core", Status.DOWN, "docker error: {}".format(exc))

    status = mgr.get_container_status()  # {service: {name,status,uptime,image}}
    actual = {
        svc: {"status": info.get("status", ""), "image": info.get("image", "")}
        for svc, info in status.items()
    }

    drift_dict = None
    drift_in_sync = None
    try:
        from syrviscore import verify

        report = verify.gather_core_drift(actual=actual)
        drift_dict = report.to_dict()
        drift_in_sync = report.in_sync
    except FileNotFoundError:
        pass  # no compose yet — can't compute drift, containers still reported
    except Exception:  # noqa: BLE001 - drift is best-effort
        pass

    running = [s for s, i in status.items() if i.get("status") == "running"]
    total = len(status)

    if total == 0:
        overall = Status.DOWN
        detail = "no core containers found"
    elif drift_in_sync is False:
        overall = Status.DEGRADED
        detail = "drift: running containers do not match compose"
    elif len(running) < total:
        overall = Status.DEGRADED
        detail = "{}/{} core containers running".format(len(running), total)
    else:
        overall = Status.OK
        detail = "{}/{} core containers running".format(len(running), total)

    return ProbeResult(
        component="core",
        status=overall,
        detail=detail,
        extra={"containers": status, "drift": drift_dict},
    )


async def probe_core(settings) -> ProbeResult:
    # DockerManager + drift are synchronous; keep the event loop free.
    return await asyncio.to_thread(_core_sync, settings)
