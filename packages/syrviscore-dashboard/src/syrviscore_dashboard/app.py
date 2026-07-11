"""FastAPI application factory for the SyrvisCore Dashboard."""

import os
from typing import Optional

from fastapi import Depends, FastAPI

from .__version__ import __version__
from .aggregator import HealthAggregator
from .api import api_router
from .auth.deps import require_user, setup_auth
from .auth.oidc import auth_router
from .settings import DashboardSettings, get_settings
from .static import mount_spa


def create_app(settings: Optional[DashboardSettings] = None) -> FastAPI:
    """Build the dashboard FastAPI app.

    Args:
        settings: injected settings (tests pass a custom object); defaults to the
            process-wide env-sourced settings.
    """
    settings = settings or get_settings()

    # syrviscore.paths reads SYRVIS_HOME from the environment; make sure it's set
    # when the operator configured it via the dashboard's own setting.
    if settings.syrvis_home:
        os.environ.setdefault("SYRVIS_HOME", settings.syrvis_home)

    app = FastAPI(
        title="SyrvisCore Dashboard",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.aggregator = HealthAggregator(settings)

    # Session middleware + provider objects + fail-closed startup checks.
    setup_auth(app, settings)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict:
        """Unauthenticated container liveness probe."""
        return {"status": "ok", "service": "syrviscore-dashboard", "version": __version__}

    # Every /api/* route requires an authenticated user; /healthz and /auth/* do not.
    app.include_router(api_router, dependencies=[Depends(require_user)])
    app.include_router(auth_router)

    # Serve the built SPA last so the API/auth routes take precedence.
    mount_spa(app, settings.dashboard_static_dir)

    return app
