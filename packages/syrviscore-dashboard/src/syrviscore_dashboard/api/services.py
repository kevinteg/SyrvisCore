"""Service listing + Layer 2 lifecycle (add/remove/start/stop/restart/update)."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import manage
from ..manage import ManagementDisabled

router = APIRouter(prefix="/api", tags=["services"])


class ServiceAddRequest(BaseModel):
    source: str
    start: bool = True


def _core_services() -> dict:
    from syrviscore.docker_manager import DockerManager

    try:
        mgr = DockerManager()
    except Exception as exc:  # noqa: BLE001 - docker may be unreachable
        return {"items": [], "error": str(exc)}
    status = mgr.get_container_status()  # {service: {name,status,uptime,image}}
    items = [dict(service=svc, **info) for svc, info in status.items()]
    return {"items": items}


def _layer2_services() -> dict:
    from syrviscore.service_manager import ServiceManager

    try:
        return {"items": ServiceManager().list()}
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "error": str(exc)}


@router.get("/services")
def list_services() -> dict:
    """Core stack containers + Layer 2 services in one call."""
    return {"core": _core_services(), "layer2": _layer2_services()}


def _run_l2(fn, *args) -> dict:
    try:
        ok, message = fn(*args)
    except ManagementDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@router.post("/services")
def add_service(payload: ServiceAddRequest, request: Request) -> dict:
    settings = request.app.state.settings
    return _run_l2(manage.layer2_add, payload.source, payload.start, settings)


@router.post("/services/{name}/{action}")
def service_action(name: str, action: str, request: Request) -> dict:
    settings = request.app.state.settings
    return _run_l2(manage.layer2_action, name, action, settings)


@router.delete("/services/{name}")
def remove_service(
    name: str, request: Request, purge: bool = Query(False, description="also delete data")
) -> dict:
    settings = request.app.state.settings
    return _run_l2(manage.layer2_remove, name, purge, settings)
