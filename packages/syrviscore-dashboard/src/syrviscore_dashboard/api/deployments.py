"""Deployment history + instance runstate — read-only library views.

The records are redacted at the source (``deployments.load_history`` masks
every inline env value; ``env_names`` carries keys only), so this router never
handles a secret. The runstate lets the SPA distinguish "intentionally halted"
from "everything crashed" — a halted instance shows exited containers that
would otherwise read as an outage.
"""

from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api", tags=["deployments"])


@router.get("/deployments")
def deployment_history(
    workload: Optional[str] = Query(None, description="one workload (or '@core')"),
    limit: int = Query(20, ge=1, le=50, description="newest N revisions per workload"),
) -> dict:
    """Per-workload deployment revisions, newest first, env values masked."""
    from syrviscore import deployments
    from syrviscore.paths import get_syrvis_home

    try:
        return deployments.load_history(get_syrvis_home(), workload=workload, limit=limit)
    except Exception as exc:  # noqa: BLE001 - read-only view degrades, never 500s
        return {"workloads": {}, "invalid": [], "error": str(exc)}


@router.get("/runstate")
def runstate() -> dict:
    """The instance runstate: {"state": "active"} or the halted record."""
    from syrviscore import lifecycle

    try:
        return lifecycle.read_runstate() or {"state": "active"}
    except Exception as exc:  # noqa: BLE001 - unreadable state reads as active
        return {"state": "active", "error": str(exc)}
