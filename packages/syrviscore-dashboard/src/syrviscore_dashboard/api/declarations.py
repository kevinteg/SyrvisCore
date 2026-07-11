"""Declared-services view — ``config/services.d`` intent vs installed reality.

Read-only by construction: the dashboard container mounts ``config`` read-only,
so it can LOAD declarations and REPORT the reconcile plan (drift) but never
apply it — convergence is the ``reconcile`` SSH action (``sudo syrvis
reconcile``).

Same degradation contract as ``/api/links`` and ``/api/routes``: any library
failure returns an ``error`` envelope with empty rows; this endpoint never 500s.
"""

from typing import Any, Dict, List

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["declarations"])


@router.get("/declarations")
def declarations() -> dict:
    """Union of declared + installed services, each with its drift ``state``."""
    try:
        from syrviscore import services_d
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        declared, invalid = services_d.load_declarations(manager.syrvis_home)
        # Read-only plan: per-declared-service docker status via the manager,
        # no prune policy, nothing mutated.
        plan = services_d.build_reconcile_plan(manager, declared, invalid)
        # Installed manifests by name (None = present but unloadable) — the
        # same view the planner diffs against, so the two can't disagree.
        installed = services_d._installed_manifests(manager)
    except Exception as exc:  # noqa: BLE001 - degrade, never 500
        return {"services": [], "invalid": [], "error": str(exc)}

    pending = {a["name"]: a["kind"] for a in plan["actions"]}
    in_sync = set(plan["in_sync"])
    disabled = set(plan["disabled"])

    def state_of(name: str) -> str:
        if name in pending:
            return "pending_{}".format(pending[name])
        if name in in_sync:
            return "in_sync"
        if name in disabled:
            return "disabled"
        return "unmanaged"

    rows: List[Dict[str, Any]] = []
    for name in sorted(set(declared) | set(installed)):
        decl = declared.get(name)
        # Declared content wins for display; fall back to the installed
        # manifest (which may be None for a broken install — surface what we
        # know rather than nothing).
        definition = decl or installed.get(name)
        traefik = definition.traefik if definition else None
        routed = bool(traefik and traefik.enabled)
        rows.append(
            {
                "name": name,
                "declared": decl is not None,
                "installed": name in installed,
                "enabled": decl.enabled if decl else None,
                "critical": decl.critical if decl else None,
                "image": definition.image if definition else None,
                "subdomain": traefik.subdomain if routed else "",
                "exposure": traefik.exposure if routed else None,
                "status": manager._get_service_status(
                    (definition.container_name if definition else "") or name
                ),
                "state": state_of(name),
            }
        )

    return {"services": rows, "invalid": invalid, "summary": plan["summary"]}
