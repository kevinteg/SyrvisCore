"""Shared fixtures: a temp SyrvisCore home + a TestClient over the app factory."""

import json

import pytest
from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app
from syrviscore_dashboard.settings import DashboardSettings


@pytest.fixture
def syrvis_home(tmp_path):
    """A minimal but realistic SYRVIS_HOME: manifest + config/.env + compose."""
    home = tmp_path / "syrviscore"
    (home / "config").mkdir(parents=True)
    (home / "data").mkdir()
    (home / "versions" / "0.2.0").mkdir(parents=True)

    (home / ".syrviscore-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "active_version": "0.2.0",
                "install_path": str(home),
                "setup_complete": True,
                "versions": {"0.2.0": {"status": "active"}},
            }
        )
    )
    (home / "config" / ".env").write_text(
        "DOMAIN=example.com\n"
        "ACME_EMAIL=admin@example.com\n"
        "NETWORK_SUBNET=192.168.0.0/24\n"
        "CLOUDFLARE_TUNNEL_TOKEN=tok-secret\n"
        "PORTAINER_ADMIN_PASSWORD=hunter2hunter2\n"
    )
    (home / "config" / "docker-compose.yaml").write_text(
        "version: '3.8'\n"
        "services:\n"
        "  traefik:\n    image: traefik:v3.6.5\n"
        "  portainer:\n    image: portainer/portainer-ce:2.33.6-alpine\n"
        "  cloudflared:\n    image: cloudflare/cloudflared:2025.11.1\n"
    )
    return home


@pytest.fixture
def make_settings(syrvis_home, monkeypatch):
    """Factory for isolated DashboardSettings (auth off by default)."""

    def _make(**overrides):
        # Isolate from any real dashboard env vars in the shell.
        for var in (
            "DASHBOARD_AUTH_MODE",
            "CLOUDFLARE_ACCESS_TEAM",
            "CLOUDFLARE_ACCESS_AUD",
            "OIDC_ISSUER",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("SYRVIS_HOME", str(syrvis_home))
        base = dict(dashboard_auth_mode="none", syrvis_home=str(syrvis_home))
        base.update(overrides)
        return DashboardSettings(**base)

    return _make


@pytest.fixture
def client(make_settings):
    """A TestClient over the app with auth disabled and a temp home."""
    return TestClient(create_app(make_settings()))
