"""Core-stack lifecycle — start/stop/restart individual core containers."""

from fastapi import APIRouter, HTTPException

from .. import manage
from ._errors import as_http

router = APIRouter(prefix="/api", tags=["management"])


@router.post("/core/{service}/{action}")
def core_action(service: str, action: str) -> dict:
    """Start/stop/restart a core container (traefik/portainer/cloudflared) via the Docker SDK."""
    try:
        ok, message = manage.core_lifecycle(service, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - docker_util errors mapped to HTTP
        raise as_http(exc)
    return {"ok": ok, "message": message}
