"""Privileged-op catalog — the dashboard hands back the SSH command, never runs it."""

from fastapi import APIRouter, Depends, HTTPException

from .. import ssh_actions
from ..deps import get_settings_dep
from ..settings import DashboardSettings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/actions")
def list_actions(settings: DashboardSettings = Depends(get_settings_dep)) -> dict:
    return {"actions": ssh_actions.catalog(settings)}


@router.post("/actions/{action_id}")
def action_command(action_id: str, settings: DashboardSettings = Depends(get_settings_dep)) -> dict:
    action = ssh_actions.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="unknown action: {}".format(action_id))
    return {
        "id": action.id,
        "ssh_command": ssh_actions.render(settings, action),
        "why_privileged": action.why_privileged,
        "note": "Run this yourself over SSH — the dashboard never executes host-root commands.",
    }
