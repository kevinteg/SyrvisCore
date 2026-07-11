"""/api/declarations contract — services.d intent vs installed state, never a 500.

The library seam is mocked the way the routes tests mock ``syrviscore.hostnames``:
``load_declarations`` / ``_installed_manifests`` are monkeypatched on
``syrviscore.services_d`` and the ServiceManager is a docker-free fake, while the
REAL ``build_reconcile_plan`` computes the drift states — so the endpoint's
state mapping is exercised against the planner's actual output shape.
"""

from pathlib import Path

import syrviscore.service_manager as service_manager_mod
import syrviscore.services_d as services_d_mod
from syrviscore.service_schema import ServiceDefinition


def _definition(name, *, image_tag="1.0.0", enabled=True, critical=False, exposure="internal"):
    return ServiceDefinition.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "image": "ghcr.io/example/{}:{}".format(name, image_tag),
            "traefik": {"enabled": True, "subdomain": name, "port": 80, "exposure": exposure},
            "enabled": enabled,
            "critical": critical,
        }
    )


class FakeManager:
    """Docker-free stand-in: only what the endpoint + planner actually touch."""

    def __init__(self, statuses=None):
        self.syrvis_home = Path("/nonexistent/syrvis-home")
        self.statuses = statuses or {}

    def _get_service_status(self, name):
        return self.statuses.get(name, "unknown")


def _wire(monkeypatch, declared, invalid, installed, statuses):
    fake = FakeManager(statuses)
    monkeypatch.setattr(service_manager_mod, "ServiceManager", lambda: fake)
    monkeypatch.setattr(services_d_mod, "load_declarations", lambda home: (declared, invalid))
    monkeypatch.setattr(services_d_mod, "_installed_manifests", lambda mgr: installed)
    return fake


def test_declarations_mixed_states(client, monkeypatch):
    """Declared/installed/unmanaged/invalid all surface, with per-name drift."""
    web = _definition("web", critical=True)  # installed, running -> in_sync
    api = _definition("api")  # declared only -> pending_add
    cache_v2 = _definition("cache", image_tag="2.0.0")  # content differs -> pending_replace
    declared = {"web": web, "api": api, "cache": cache_v2}
    installed = {
        "web": web,
        "cache": _definition("cache", image_tag="1.0.0"),
        "legacy": _definition("legacy", exposure="tunnel"),  # not declared -> unmanaged
    }
    invalid = [{"file": "broken.yaml", "error": "declaration must be a mapping"}]
    statuses = {"web": "running", "cache": "running", "legacy": "exited"}
    _wire(monkeypatch, declared, invalid, installed, statuses)

    resp = client.get("/api/declarations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invalid"] == invalid
    assert body["summary"] == {"declared": 3, "invalid": 1, "total_actions": 2, "destructive": 0}

    by_name = {s["name"]: s for s in body["services"]}
    # The UNION of declared + installed, sorted for a stable UI.
    assert [s["name"] for s in body["services"]] == ["api", "cache", "legacy", "web"]

    assert by_name["web"]["state"] == "in_sync"
    assert by_name["web"] == {
        "name": "web",
        "declared": True,
        "installed": True,
        "enabled": True,
        "critical": True,  # the critical flag surfaces
        "image": "ghcr.io/example/web:1.0.0",
        "subdomain": "web",
        "exposure": "internal",
        "status": "running",
        "state": "in_sync",
    }

    # Declared but not installed -> the plan's add action, no container yet.
    assert by_name["api"]["state"] == "pending_add"
    assert by_name["api"]["installed"] is False
    assert by_name["api"]["status"] == "unknown"

    # Content drift -> replace; the DECLARED image is what's shown.
    assert by_name["cache"]["state"] == "pending_replace"
    assert by_name["cache"]["image"] == "ghcr.io/example/cache:2.0.0"

    # Installed with no declaration -> unmanaged; orchestration flags are null.
    legacy = by_name["legacy"]
    assert legacy["state"] == "unmanaged"
    assert legacy["declared"] is False
    assert legacy["installed"] is True
    assert legacy["enabled"] is None
    assert legacy["critical"] is None
    assert legacy["image"] == "ghcr.io/example/legacy:1.0.0"
    assert legacy["exposure"] == "tunnel"


def test_declarations_disabled_states(client, monkeypatch):
    """enabled=false: quiescent -> disabled; still running -> pending_stop."""
    off = _definition("off", enabled=False)
    running_off = _definition("runningoff", enabled=False)
    declared = {"off": off, "runningoff": running_off}
    installed = {"off": off, "runningoff": running_off}
    statuses = {"off": "exited", "runningoff": "running"}
    _wire(monkeypatch, declared, [], installed, statuses)

    body = client.get("/api/declarations").json()
    by_name = {s["name"]: s for s in body["services"]}
    assert by_name["off"]["state"] == "disabled"
    assert by_name["off"]["enabled"] is False
    assert by_name["runningoff"]["state"] == "pending_stop"
    assert by_name["runningoff"]["enabled"] is False


def test_declarations_library_failure_degrades(client, monkeypatch):
    """A blowup in the loader is a degraded envelope, not a 500."""
    fake = FakeManager()
    monkeypatch.setattr(service_manager_mod, "ServiceManager", lambda: fake)

    def boom(home):
        raise RuntimeError("services.d exploded")

    monkeypatch.setattr(services_d_mod, "load_declarations", boom)

    resp = client.get("/api/declarations")
    assert resp.status_code == 200
    assert resp.json() == {"services": [], "invalid": [], "error": "services.d exploded"}


def test_declarations_manager_unavailable_degrades(client, monkeypatch):
    """Even the ServiceManager constructor failing never 500s."""

    def no_manager():
        raise RuntimeError("SYRVIS_HOME gone")

    monkeypatch.setattr(service_manager_mod, "ServiceManager", no_manager)

    resp = client.get("/api/declarations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["services"] == []
    assert body["error"] == "SYRVIS_HOME gone"


def test_reconcile_ssh_action_in_catalog(client):
    """The converge step is an SSH action — the dashboard only reports drift."""
    body = client.get("/api/system/actions").json()
    actions = {a["id"]: a for a in body["actions"]}
    assert "reconcile" in actions
    assert actions["reconcile"]["ssh_command"] == "ssh nas 'sudo syrvis reconcile'"
    assert "services.d" in actions["reconcile"]["why_privileged"]
