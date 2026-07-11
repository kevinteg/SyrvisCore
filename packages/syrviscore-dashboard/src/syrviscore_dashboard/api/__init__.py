"""Aggregate all API routers into one for the app factory to mount."""

from fastapi import APIRouter

from . import config_routes, core, events, health, logs, me, services, system

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(config_routes.router)
api_router.include_router(services.router)
api_router.include_router(core.router)
api_router.include_router(system.router)
api_router.include_router(events.router)
api_router.include_router(logs.router)
api_router.include_router(me.router)

__all__ = ["api_router"]
