"""Folded platform summary — ``GET /api/summary`` (schema ``syrvis-summary/v1``).

One small, read-only JSON that answers a single question for an *external*
umbrella console: **is this platform worth diving into right now?** It is an
AGGREGATION of what the dashboard already knows — the TTL-cached health
snapshot (core containers + core drift + Traefik overview + tunnel readiness +
config), the instance runstate, the Layer 2 services model, the deployment
history, and the update caches. It starts **no new probes** and makes **no
outbound network call**, so it is cheap enough to poll continuously.

Deliberately excluded: the full ``verify`` validator report (``/api/verify``).
Run from inside this container it reports ``healthy: false`` for "Python venv:
Not found" — a container-context false negative — so folding it in would paint
a healthy platform red. (The drift *gatherers* in ``syrviscore.verify`` are a
different thing and are used here; the validator suite is not.)

What the numbers mean:

- ``unhealthy`` is the list of declared containers whose Docker state is not
  ``running``. This data path carries container state, not Docker healthcheck
  results, so "unhealthy" means "not running as declared" — nothing finer is
  claimed.
- ``drift.items`` counts **failing** drift items only, so ``items == 0`` always
  agrees with ``in_sync``.
- Any block whose source is unavailable is ``null`` rather than a fabricated
  zero (absent beats invented). Consumers must null-check.

The fold (severity is the max over every triggered rule):

- ``down``     — a core container is not running, core state is unreadable, or
  the instance runstate is ``halted``.
- ``degraded`` — core or L2 drift, an L2 service not running, a maintenance
  hold, or the tunnel is enabled with zero ready edge connections.
- ``ok``       — none of the above.

Pending container-image updates are **informational only** and never degrade
the state.

No secrets and nothing site-specific cross this endpoint: the configured
domain, hostnames, tokens and env values are all left behind in the sources.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import APIRouter, Depends

from ..__version__ import __version__ as _dashboard_version
from ..aggregator import HealthAggregator
from ..deps import get_aggregator

router = APIRouter(prefix="/api", tags=["summary"])

SCHEMA = "syrvis-summary/v1"

#: How many names a problem phrase spells out before collapsing to a count.
_NAME_CAP = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phrase(prefix: str, names: List[str], total: int) -> str:
    """One problem phrase: names while few, a count once the list gets long."""
    if len(names) <= _NAME_CAP:
        return "{}: {}".format(prefix, ", ".join(names))
    return "{}: {} of {}".format(prefix, len(names), total)


def count_containers(statuses: Mapping[str, str]) -> Dict[str, Any]:
    """Fold a ``{name: docker_state}`` map into the contract's counts block."""
    unhealthy = sorted(name for name, state in statuses.items() if state != "running")
    return {
        "desired": len(statuses),
        "running": len(statuses) - len(unhealthy),
        "unhealthy": unhealthy,
    }


def fold(
    containers: Mapping[str, Optional[Dict[str, Any]]],
    drift: Mapping[str, Any],
    runstate: Mapping[str, Any],
    tunnel: Mapping[str, Any],
) -> Tuple[str, str]:
    """Reduce the assembled blocks to ``(state, detail)``.

    Pure and side-effect free — the unit tests drive this directly with
    hand-built blocks, no Docker and no HTTP.
    """
    core = containers.get("core")
    layer2 = containers.get("l2")

    down: List[str] = []
    degraded: List[str] = []

    # --- down: the platform is not serving -----------------------------------
    if core is None:
        down.append("core container state unreadable")
    elif core["desired"] == 0:
        down.append("no core containers found")
    elif core["unhealthy"]:
        down.append(_phrase("core not running", core["unhealthy"], core["desired"]))

    state_name = (runstate or {}).get("state") or "active"
    reason = (runstate or {}).get("reason")
    if state_name == "halted":
        down.append("halted ({})".format(reason or "reason unrecorded"))

    # --- degraded: serving, but something wants an operator ------------------
    # A maintenance hold is its own signal; it reads as `down` above only
    # because halting stops the core stack too.
    if reason == "maintenance":
        degraded.append("maintenance hold")
    if drift.get("core_in_sync") is False:
        degraded.append("core drift")
    if drift.get("l2_in_sync") is False:
        degraded.append("L2 drift")
    if layer2 and layer2["unhealthy"]:
        degraded.append(_phrase("L2 not running", layer2["unhealthy"], layer2["desired"]))
    if tunnel.get("enabled") and not (tunnel.get("ready_connections") or 0):
        degraded.append("tunnel has no ready edge connections")

    state = "down" if down else ("degraded" if degraded else "ok")

    parts: List[str] = []
    parts.append(
        "{}/{} core".format(core["running"], core["desired"]) if core else "core state unknown"
    )
    if layer2 is not None:
        parts.append("{}/{} L2 running".format(layer2["running"], layer2["desired"]))
    problems = down + degraded
    if problems:
        parts.extend(problems)
    elif drift.get("core_in_sync") is True:
        parts.append("no drift")
    else:
        parts.append("drift unknown")

    return state, " · ".join(parts)


# --- source readers -----------------------------------------------------------
# Each returns None when its source can't answer, and never raises.


def _core_block(core_component: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    containers = (core_component.get("extra") or {}).get("containers")
    if not isinstance(containers, dict):
        return None  # docker unreachable — the core probe already says `down`
    return count_containers(
        {name: (info or {}).get("status", "") for name, info in containers.items()}
    )


def _core_drift(core_component: Mapping[str, Any]) -> Tuple[Optional[bool], int]:
    report = (core_component.get("extra") or {}).get("drift")
    if not isinstance(report, dict):
        return None, 0
    items = report.get("items") or []
    failing = sum(1 for item in items if isinstance(item, dict) and item.get("failure"))
    return report.get("in_sync"), failing


def _traefik_block(component: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    routers = (component.get("extra") or {}).get("routers")
    if not isinstance(routers, dict):
        return None  # API unreachable — absent beats invented
    return {"routers": routers.get("total"), "errors": routers.get("errors")}


def _tunnel_block(component: Mapping[str, Any]) -> Dict[str, Any]:
    if not component or component.get("status") == "not_configured":
        return {"enabled": False, "ready_connections": None}
    ready = (component.get("extra") or {}).get("readyConnections")
    return {"enabled": True, "ready_connections": ready}


def _runstate_sync() -> Dict[str, Any]:
    from syrviscore import lifecycle

    try:
        return lifecycle.read_runstate() or {"state": "active"}
    except Exception:  # noqa: BLE001 - unreadable state fails safe to active
        return {"state": "active"}


def _layer2_sync() -> Optional[Dict[str, Any]]:
    """L2 counts from the services model (the same view ``/api/services`` shows)."""
    from .services import _layer2_services

    data = _layer2_services()
    if data.get("error"):
        return None
    items = data.get("items") or []
    return count_containers({item.get("name", "?"): item.get("status", "") for item in items})


def _layer2_drift_sync() -> Tuple[Optional[bool], int]:
    """Declared-intent L2 drift. ``gather_l2_drift`` returns None when unknowable."""
    from syrviscore import verify

    try:
        report = verify.gather_l2_drift()
    except Exception:  # noqa: BLE001 - best effort, like the core probe's drift
        return None, 0
    if report is None:
        return None, 0
    return report.in_sync, len(report.failures)


def _last_deploy_sync() -> Optional[Dict[str, Any]]:
    """The newest deployment revision across every workload.

    Reads the same REDACTED history ``/api/deployments`` serves, so no env
    value is ever in scope here — and only three scalars are carried out.
    """
    from syrviscore import deployments
    from syrviscore.paths import get_syrvis_home

    try:
        history = deployments.load_history(get_syrvis_home(), limit=1)
    except Exception:  # noqa: BLE001 - no home / unreadable history
        return None
    newest: Optional[Dict[str, Any]] = None
    for name, records in (history.get("workloads") or {}).items():
        if not records:
            continue
        record = records[0]
        at = record.get("timestamp")
        if not at:
            continue
        if newest is None or at > newest["at"]:
            newest = {
                "at": at,
                "target": record.get("workload") or name,
                "revision": record.get("revision"),
            }
    return newest


def _version_sync(active_version: Optional[str]) -> Dict[str, Any]:
    """Versions + update counters, read from caches only (never the network).

    ``update_available`` and ``image_updates`` are ``null`` until something has
    populated those caches (``/api/updates``, or the CLI/MCP update check).
    A summary poll must not be the thing that reaches out to GitHub or a
    registry, so a cold cache reports "unknown" rather than a guess.
    """
    from .updates import cached_platform_version

    update_available: Optional[bool] = None
    cached = cached_platform_version()
    if cached is not None:
        update_available = cached.get("update_available")

    image_updates: Optional[int] = None
    try:
        from syrviscore import image_updates as iu

        report = iu.read_cached()
        if isinstance(report, dict):
            image_updates = report.get("update_count")
    except Exception:  # noqa: BLE001 - no cache / library unavailable
        pass

    return {
        "active": active_version,
        "dashboard": _dashboard_version,
        "update_available": update_available,
        "image_updates": image_updates,
    }


# --- the endpoint -------------------------------------------------------------


@router.get("/summary")
async def summary(agg: HealthAggregator = Depends(get_aggregator)) -> dict:
    """The folded platform summary (``syrvis-summary/v1``)."""
    snapshot = await agg.get_snapshot()
    components = snapshot.get("components") or {}
    core_component = components.get("core") or {}
    config_extra = (components.get("config") or {}).get("extra") or {}

    # The library reads are synchronous; run them off the event loop, together.
    runstate, layer2, l2_drift, last_deploy, version = await asyncio.gather(
        asyncio.to_thread(_runstate_sync),
        asyncio.to_thread(_layer2_sync),
        asyncio.to_thread(_layer2_drift_sync),
        asyncio.to_thread(_last_deploy_sync),
        asyncio.to_thread(_version_sync, config_extra.get("active_version")),
    )

    core_in_sync, core_items = _core_drift(core_component)
    l2_in_sync, l2_items = l2_drift
    containers = {"core": _core_block(core_component), "l2": layer2}
    drift = {
        "core_in_sync": core_in_sync,
        "l2_in_sync": l2_in_sync,
        "items": core_items + l2_items,
    }
    tunnel = _tunnel_block(components.get("cloudflared") or {})

    state, detail = fold(containers, drift, runstate, tunnel)

    return {
        "schema": SCHEMA,
        "generated_at": _utc_now(),
        "state": state,
        "detail": detail,
        "runstate": runstate.get("state") or "active",
        "version": version,
        "containers": containers,
        "drift": drift,
        "tunnel": tunnel,
        "traefik": _traefik_block(components.get("traefik") or {}),
        "last_deploy": last_deploy,
    }
