"""/api/summary — the folded platform summary (``syrvis-summary/v1``).

Two layers: the pure ``fold`` (severity + detail line, no Docker, no HTTP) and
the endpoint contract (always answers, honest nulls, never a secret).
"""

from fastapi.testclient import TestClient

from syrviscore_dashboard.api import summary as summary_api
from syrviscore_dashboard.app import create_app

CONTRACT_KEYS = {
    "schema",
    "generated_at",
    "state",
    "detail",
    "runstate",
    "version",
    "containers",
    "drift",
    "tunnel",
    "traefik",
    "last_deploy",
}


# --- helpers ------------------------------------------------------------------


def _counts(desired, running, unhealthy=()):
    return {"desired": desired, "running": running, "unhealthy": list(unhealthy)}


def _blocks(**overrides):
    """A healthy set of fold inputs; override one thing per test."""
    base = dict(
        containers={"core": _counts(4, 4), "l2": _counts(26, 26)},
        drift={"core_in_sync": True, "l2_in_sync": True, "items": 0},
        runstate={"state": "active"},
        tunnel={"enabled": True, "ready_connections": 4},
    )
    base.update(overrides)
    return base


class _FakeAggregator:
    """Stands in for the TTL-cached health snapshot."""

    def __init__(self, components):
        self.components = components

    async def get_snapshot(self, force: bool = False) -> dict:
        return {
            "generated_at": "2026-08-12T03:34:04+00:00",
            "overall": "ok",
            "healthy": True,
            "components": self.components,
        }


def _healthy_components():
    """Shaped exactly like a live snapshot (core/traefik/cloudflared/config)."""
    return {
        "core": {
            "component": "core",
            "status": "ok",
            "detail": "4/4 core containers running",
            "extra": {
                "containers": {
                    "traefik": {"status": "running", "image": "traefik:v3.6.5"},
                    "portainer": {"status": "running", "image": "portainer:2.44.0"},
                    "cloudflared": {"status": "running", "image": "cloudflared:2026.7.3"},
                    "syrviscore-dashboard": {"status": "running", "image": "dashboard:x"},
                },
                "drift": {"scope": "core", "in_sync": True, "items": []},
            },
        },
        "traefik": {
            "component": "traefik",
            "status": "ok",
            "extra": {"routers": {"total": 26, "warnings": 0, "errors": 0}},
        },
        "cloudflared": {
            "component": "cloudflared",
            "status": "ok",
            "extra": {"readyConnections": 4, "connectorId": "abc"},
        },
        "config": {
            "component": "config",
            "status": "ok",
            "extra": {"domain": "example.com", "active_version": "0.5.7"},
        },
    }


def _client_with(make_settings, monkeypatch, components=None, **sources):
    """A TestClient whose snapshot + library reads are all injected."""
    app = create_app(make_settings())
    app.state.aggregator = _FakeAggregator(components or _healthy_components())

    defaults = {
        "_runstate_sync": lambda: {"state": "active"},
        "_layer2_sync": lambda: _counts(26, 26),
        "_layer2_drift_sync": lambda: (True, 0),
        "_last_deploy_sync": lambda: {
            "at": "2026-08-11T16:20:11Z",
            "target": "immich-server",
            "revision": 12,
        },
        "_version_sync": lambda active: {
            "active": active,
            "dashboard": "0.5.8",
            "update_available": False,
            "image_updates": 7,
        },
    }
    defaults.update(sources)
    for name, fn in defaults.items():
        monkeypatch.setattr(summary_api, name, fn)
    return TestClient(app)


# --- the fold -----------------------------------------------------------------


def test_fold_healthy_is_ok_with_the_counts_line():
    state, detail = summary_api.fold(**_blocks())
    assert state == "ok"
    assert detail == "4/4 core · 26/26 L2 running · no drift"


def test_fold_core_not_running_is_down():
    state, detail = summary_api.fold(
        **_blocks(containers={"core": _counts(4, 3, ["traefik"]), "l2": _counts(26, 26)})
    )
    assert state == "down"
    assert "core not running: traefik" in detail
    assert detail.startswith("3/4 core")


def test_fold_unreadable_core_is_down():
    state, detail = summary_api.fold(**_blocks(containers={"core": None, "l2": None}))
    assert state == "down"
    assert detail == "core state unknown · core container state unreadable"


def test_fold_no_core_containers_is_down():
    state, detail = summary_api.fold(**_blocks(containers={"core": _counts(0, 0), "l2": None}))
    assert state == "down"
    assert "no core containers found" in detail


def test_fold_halted_is_down_and_names_the_reason():
    state, detail = summary_api.fold(**_blocks(runstate={"state": "halted", "reason": "ups"}))
    assert state == "down"
    assert "halted (ups)" in detail


def test_fold_maintenance_hold_is_reported_alongside_the_halt():
    state, detail = summary_api.fold(
        **_blocks(runstate={"state": "halted", "reason": "maintenance"})
    )
    assert state == "down"  # halting stops the core stack; the hold is the why
    assert "halted (maintenance)" in detail and "maintenance hold" in detail


def test_fold_maintenance_without_a_halt_only_degrades():
    state, detail = summary_api.fold(
        **_blocks(runstate={"state": "active", "reason": "maintenance"})
    )
    assert state == "degraded"
    assert "maintenance hold" in detail


def test_fold_core_drift_degrades():
    state, detail = summary_api.fold(
        **_blocks(drift={"core_in_sync": False, "l2_in_sync": True, "items": 1})
    )
    assert state == "degraded"
    assert "core drift" in detail


def test_fold_l2_drift_degrades():
    state, detail = summary_api.fold(
        **_blocks(drift={"core_in_sync": True, "l2_in_sync": False, "items": 2})
    )
    assert state == "degraded"
    assert "L2 drift" in detail


def test_fold_l2_not_running_degrades_never_downs():
    state, detail = summary_api.fold(
        **_blocks(containers={"core": _counts(4, 4), "l2": _counts(26, 25, ["grafana"])})
    )
    assert state == "degraded"
    assert "L2 not running: grafana" in detail


def test_fold_many_unhealthy_names_collapse_to_a_count():
    unhealthy = ["a", "b", "c", "d", "e"]
    _, detail = summary_api.fold(
        **_blocks(containers={"core": _counts(4, 4), "l2": _counts(26, 21, unhealthy)})
    )
    assert "L2 not running: 5 of 26" in detail


def test_fold_starved_tunnel_degrades():
    state, detail = summary_api.fold(**_blocks(tunnel={"enabled": True, "ready_connections": 0}))
    assert state == "degraded"
    assert "tunnel has no ready edge connections" in detail


def test_fold_disabled_tunnel_is_not_a_problem():
    state, _ = summary_api.fold(**_blocks(tunnel={"enabled": False, "ready_connections": None}))
    assert state == "ok"


def test_fold_unknown_drift_is_not_claimed_as_clean():
    state, detail = summary_api.fold(
        **_blocks(drift={"core_in_sync": None, "l2_in_sync": None, "items": 0})
    )
    assert state == "ok"  # unknown is not a failure
    assert detail.endswith("drift unknown")


def test_fold_down_outranks_degraded():
    state, _ = summary_api.fold(
        **_blocks(
            containers={"core": _counts(4, 3, ["traefik"]), "l2": _counts(26, 25, ["grafana"])},
            drift={"core_in_sync": False, "l2_in_sync": False, "items": 3},
        )
    )
    assert state == "down"


def test_count_containers_folds_states():
    counts = summary_api.count_containers(
        {"a": "running", "b": "exited", "c": "running", "d": "restarting"}
    )
    assert counts == {"desired": 4, "running": 2, "unhealthy": ["b", "d"]}


# --- the endpoint -------------------------------------------------------------


def test_summary_contract_shape(make_settings, monkeypatch):
    client = _client_with(make_settings, monkeypatch)
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == CONTRACT_KEYS
    assert body["schema"] == "syrvis-summary/v1"
    assert body["generated_at"].endswith("Z")
    assert body["state"] == "ok"
    assert body["detail"] == "4/4 core · 26/26 L2 running · no drift"
    assert body["runstate"] == "active"
    assert body["version"] == {
        "active": "0.5.7",
        "dashboard": "0.5.8",
        "update_available": False,
        "image_updates": 7,
    }
    assert body["containers"]["core"] == {"desired": 4, "running": 4, "unhealthy": []}
    assert body["containers"]["l2"] == {"desired": 26, "running": 26, "unhealthy": []}
    assert body["drift"] == {"core_in_sync": True, "l2_in_sync": True, "items": 0}
    assert body["tunnel"] == {"enabled": True, "ready_connections": 4}
    assert body["traefik"] == {"routers": 26, "errors": 0}
    assert body["last_deploy"] == {
        "at": "2026-08-11T16:20:11Z",
        "target": "immich-server",
        "revision": 12,
    }


def test_summary_carries_no_site_specific_facts(make_settings, monkeypatch):
    """The config probe knows the domain; the summary must not repeat it."""
    client = _client_with(make_settings, monkeypatch)
    assert "example.com" not in client.get("/api/summary").text


def test_summary_image_updates_never_degrade(make_settings, monkeypatch):
    client = _client_with(
        make_settings,
        monkeypatch,
        _version_sync=lambda active: {
            "active": active,
            "dashboard": "0.5.8",
            "update_available": True,
            "image_updates": 7,
        },
    )
    body = client.get("/api/summary").json()
    assert body["version"]["image_updates"] == 7
    assert body["version"]["update_available"] is True
    assert body["state"] == "ok"


def test_summary_reads_container_state_from_the_snapshot(make_settings, monkeypatch):
    components = _healthy_components()
    components["core"]["extra"]["containers"]["traefik"]["status"] = "exited"
    client = _client_with(make_settings, monkeypatch, components=components)
    body = client.get("/api/summary").json()
    assert body["state"] == "down"
    assert body["containers"]["core"]["unhealthy"] == ["traefik"]
    assert body["containers"]["core"]["running"] == 3


def test_summary_halted_instance_is_down(make_settings, monkeypatch):
    client = _client_with(
        make_settings,
        monkeypatch,
        _runstate_sync=lambda: {"state": "halted", "reason": "maintenance", "at": "2026-08-11"},
    )
    body = client.get("/api/summary").json()
    assert body["state"] == "down"
    assert body["runstate"] == "halted"
    assert "halted (maintenance)" in body["detail"]


def test_summary_drift_counts_only_failures(make_settings, monkeypatch):
    components = _healthy_components()
    components["core"]["extra"]["drift"] = {
        "scope": "core",
        "in_sync": False,
        "items": [
            {"service": "traefik", "kind": "stopped", "failure": True},
            {"service": "stray", "kind": "unexpected", "failure": False},
        ],
    }
    client = _client_with(
        make_settings, monkeypatch, components=components, _layer2_drift_sync=lambda: (False, 2)
    )
    body = client.get("/api/summary").json()
    assert body["drift"] == {"core_in_sync": False, "l2_in_sync": False, "items": 3}
    assert body["state"] == "degraded"


def test_summary_absent_sources_are_null_not_invented(make_settings, monkeypatch):
    """Docker down, no L2 model, no history: nulls, not zeros — and still a 200."""
    components = {
        "core": {"component": "core", "status": "down", "detail": "docker unreachable"},
        "traefik": {"component": "traefik", "status": "down"},
        "cloudflared": {"component": "cloudflared", "status": "not_configured"},
        "config": {"component": "config", "status": "not_configured"},
    }
    client = _client_with(
        make_settings,
        monkeypatch,
        components=components,
        _layer2_sync=lambda: None,
        _layer2_drift_sync=lambda: (None, 0),
        _last_deploy_sync=lambda: None,
        _version_sync=lambda active: {
            "active": active,
            "dashboard": "0.5.8",
            "update_available": None,
            "image_updates": None,
        },
    )
    body = client.get("/api/summary").json()
    assert body["state"] == "down"
    assert body["containers"] == {"core": None, "l2": None}
    assert body["traefik"] is None
    assert body["last_deploy"] is None
    assert body["tunnel"] == {"enabled": False, "ready_connections": None}
    assert body["drift"] == {"core_in_sync": None, "l2_in_sync": None, "items": 0}
    assert body["version"]["update_available"] is None
    assert body["version"]["image_updates"] is None


def test_summary_survives_a_bare_environment(client):
    """No fakes at all: no docker, empty home. It must still answer honestly."""
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == CONTRACT_KEYS
    assert body["schema"] == "syrvis-summary/v1"
    assert body["state"] == "down"  # docker is unreachable in tests
    assert body["containers"]["core"] is None
    assert body["version"]["dashboard"]


def test_summary_requires_auth(make_settings):
    """It is a read-only view, but it is still behind the /api/* auth gate."""
    app = create_app(
        make_settings(
            dashboard_auth_mode="cloudflare",
            cloudflare_access_team="team",
            cloudflare_access_aud="the-aud",
        )
    )
    assert TestClient(app).get("/api/summary").status_code == 401


# --- version block ------------------------------------------------------------


def test_version_block_reads_caches_only(monkeypatch):
    from syrviscore_dashboard.api import updates as updates_api

    monkeypatch.setattr(updates_api, "cached_platform_version", lambda: None)
    block = summary_api._version_sync("0.5.7")
    assert block["active"] == "0.5.7"
    assert block["update_available"] is None  # cold cache: unknown, not "no"

    monkeypatch.setattr(updates_api, "cached_platform_version", lambda: {"update_available": True})
    assert summary_api._version_sync("0.5.7")["update_available"] is True
