"""Tests for the auto-generated Grafana dashboard (syrvis dashboard generate)."""

import json

import pytest

from syrviscore import dashboard as d
from syrviscore.service_schema import ServiceDefinition, ServiceValidationError


# ---------------------------------------------------------------------------
# build_dashboard — the pure projection (no SYRVIS_HOME needed)
# ---------------------------------------------------------------------------
def _exprs(model):
    return [t["expr"] for p in model["panels"] for t in p.get("targets", [])]


def _rows(model):
    return [p for p in model["panels"] if p["type"] == "row"]


def test_valid_grafana_shape_and_unique_ids():
    svcs = [
        d.DashService("traefik", "traefik", kind="core", critical=True),
        d.DashService("immich-server", "immich_server", kind="service"),
    ]
    model = d.build_dashboard(svcs)
    # Serializes cleanly and carries the generated-marker.
    json.loads(d.to_json(model))
    assert model["__syrviscore"]["generated"] is True
    assert model["schemaVersion"] == 39
    # Panel ids are unique and every panel fits the 24-col grid at y>=0.
    ids = [p["id"] for p in model["panels"]]
    assert len(ids) == len(set(ids))
    for p in model["panels"]:
        g = p["gridPos"]
        assert g["y"] >= 0 and 0 <= g["x"] and g["x"] + g["w"] <= 24


def test_overview_row_plus_one_row_per_service():
    svcs = [
        d.DashService("traefik", "traefik", kind="core", critical=True),
        d.DashService("portainer", "portainer", kind="core", critical=True),
        d.DashService("vmagent", "vmagent", kind="service"),
    ]
    rows = _rows(d.build_dashboard(svcs))
    titles = [r["title"] for r in rows]
    assert titles[0] == "SyrvisCore — overview"
    assert len(rows) == 1 + len(svcs)  # overview + per-service
    assert any(t.startswith("traefik — traefik") for t in titles)
    assert any(t.startswith("vmagent — vmagent") for t in titles)


def test_overview_scoped_to_the_syrvis_set():
    svcs = [d.DashService("a", "aa"), d.DashService("b", "bb")]
    exprs = _exprs(d.build_dashboard(svcs))
    # The overview aggregates ONLY the declared set, never every container.
    assert any('docker_container_running{name=~"aa|bb"}' in e for e in exprs)


def test_empty_set_matches_nothing_not_everything():
    exprs = _exprs(d.build_dashboard([]))
    # A match-nothing selector (never a bare metric that would sum the estate).
    assert any('name=~"$^"' in e for e in exprs)
    assert not any(e.strip() == "sum(docker_container_running{})" for e in exprs)


def test_per_service_panels_keyed_by_container_name():
    svcs = [d.DashService("immich", "immich_server")]
    exprs = _exprs(d.build_dashboard(svcs))
    assert any('docker_container_running{name="immich_server"}' in e for e in exprs)
    assert any(
        'time() - docker_container_started_at_seconds{name="immich_server"}' in e for e in exprs
    )
    assert any(
        'increase(docker_container_restart_count{name="immich_server"}[1h])' in e for e in exprs
    )


def test_custom_panel_container_token_substituted():
    svcs = [
        d.DashService(
            "immich",
            "immich_server",
            panels=[
                {"title": "Jobs", "expr": 'immich_jobs{name="${container}"}', "kind": "timeseries"}
            ],
        )
    ]
    exprs = _exprs(d.build_dashboard(svcs))
    assert 'immich_jobs{name="immich_server"}' in exprs
    assert not any("${container}" in e for e in exprs)


def test_collector_contract_is_overridable():
    svcs = [d.DashService("a", "aa")]
    model = d.build_dashboard(svcs, d.Collector(datasource_uid="mimir", running="cadvisor_up"))
    # Datasource UID propagates to every panel.
    assert all(p["datasource"]["uid"] == "mimir" for p in model["panels"] if p["type"] != "row")
    assert any("cadvisor_up{" in e for e in _exprs(model))


def test_row_tags_reflect_service_metadata():
    svcs = [
        d.DashService("infra-exp", "infra_exp", kind="service", tier="infra", critical=True),
        d.DashService("off", "off", kind="service", enabled=False),
    ]
    titles = [r["title"] for r in _rows(d.build_dashboard(svcs))]
    assert any("critical" in t and "infra" in t for t in titles)
    assert any("disabled" in t for t in titles)


# ---------------------------------------------------------------------------
# collect_services — integration with the declared core stack + L2 manifests
# ---------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    return h


def test_collect_includes_core_and_layer2(home, monkeypatch):
    (home / "config" / ".env").write_text("DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n")
    monkeypatch.setenv("DOMAIN", "example.com")
    from syrviscore.service_manager import ServiceManager

    ServiceManager(syrvis_home=home).add_image(
        "cyberquill", "ghcr.io/acme/cyberquill:1.4.0", exposure="tunnel", port=8080, start=False
    )
    services = d.collect_services()
    names = {s.name for s in services}
    # Primordial core is always present; the L2 service is enumerated.
    assert {"traefik", "portainer"}.issubset(names)
    assert "cyberquill" in names
    cq = next(s for s in services if s.name == "cyberquill")
    assert cq.kind == "service"
    core = next(s for s in services if s.name == "traefik")
    assert core.kind == "core" and core.critical is True


def test_generate_degrades_without_home(monkeypatch, tmp_path):
    # No resolvable SYRVIS_HOME -> a still-valid dashboard (just the overview).
    monkeypatch.delenv("SYRVIS_HOME", raising=False)
    monkeypatch.setenv("DSM_SIM_ACTIVE", "0")
    model = d.generate()
    assert _rows(model)[0]["title"] == "SyrvisCore — overview"
    json.loads(d.to_json(model))


# ---------------------------------------------------------------------------
# Schema — the dashboard: block validation + round-trip
# ---------------------------------------------------------------------------
def _svc(**extra):
    base = {"name": "svc", "version": "1", "image": "repo/app:1.0"}
    base.update(extra)
    return base


def test_dashboard_block_accepted_and_round_trips():
    sd = ServiceDefinition.from_dict(
        _svc(
            dashboard={
                "panels": [
                    {
                        "title": "Queue",
                        "expr": 'app_q{name="${container}"}',
                        "kind": "timeseries",
                        "unit": "short",
                    }
                ]
            }
        )
    )
    assert sd.dashboard["panels"][0]["title"] == "Queue"
    # Survives a to_dict -> from_dict round-trip.
    again = ServiceDefinition.from_dict(sd.to_dict())
    assert again.dashboard == sd.dashboard


def test_dashboard_block_allows_dollar_token():
    # '$' is banned in command/volumes but MUST be allowed here (${container}).
    sd = ServiceDefinition.from_dict(
        _svc(dashboard={"panels": [{"title": "t", "expr": 'm{name="${container}"}'}]})
    )
    assert "${container}" in sd.dashboard["panels"][0]["expr"]


def test_dashboard_block_rejects_unknown_keys():
    with pytest.raises(ServiceValidationError):
        ServiceDefinition.from_dict(_svc(dashboard={"panels": [], "bogus": 1}))
    with pytest.raises(ServiceValidationError):
        ServiceDefinition.from_dict(
            _svc(dashboard={"panels": [{"title": "t", "expr": "x", "color": "red"}]})
        )


def test_dashboard_block_rejects_bad_kind_and_control_chars():
    with pytest.raises(ServiceValidationError):
        ServiceDefinition.from_dict(
            _svc(dashboard={"panels": [{"title": "t", "expr": "x", "kind": "heatmap"}]})
        )
    with pytest.raises(ServiceValidationError):
        ServiceDefinition.from_dict(
            _svc(dashboard={"panels": [{"title": "t", "expr": "x\ny"}]})  # newline
        )


def test_dashboard_block_enforces_panel_cap():
    panels = [{"title": "t%d" % i, "expr": "x"} for i in range(13)]
    with pytest.raises(ServiceValidationError):
        ServiceDefinition.from_dict(_svc(dashboard={"panels": panels}))
