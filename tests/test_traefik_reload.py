"""
Tests for the change-aware Traefik reload (the deploy-storm fix).

The old behavior restarted the edge proxy unconditionally at the end of EVERY
deploy — a 12-service stack sweep bounced Traefik 12 times — and with the
caller's 10s stop timeout equal to Traefik's default graceTimeOut the drain
lost the race every time, so each bounce ended in
panic("Timeout while stopping traefik") instead of a clean exit.

These tests pin the fix: write_config reports whether the routing state Traefik
sees actually changed, deploy paths reload only on a real change, and the
generated static config carries a graceTimeOut strictly below the callers'
30s stop timeout.
"""

import os

import yaml

from syrviscore.bundle import DeployBundle
from syrviscore.service_schema import ServiceDefinition
from syrviscore.traefik_config import (
    ServiceTraefikConfig,
    generate_traefik_static_config,
)


def _svc(routed=True, subdomain="app", port=8080):
    m = {"name": "app", "version": "1", "image": "nginx:1.25.0"}
    if routed:
        m["traefik"] = {"subdomain": subdomain, "port": port}
    return ServiceDefinition.from_dict(m)


class TestWriteConfigChangeAware:
    def test_first_write_reports_change(self, tmp_path):
        tc = ServiceTraefikConfig(config_dir=tmp_path)
        path, changed = tc.write_config(_svc(), "example.com")
        assert path is not None and path.exists()
        assert changed is True

    def test_identical_rewrite_is_a_noop(self, tmp_path):
        tc = ServiceTraefikConfig(config_dir=tmp_path)
        tc.write_config(_svc(), "example.com")
        path, changed = tc.write_config(_svc(), "example.com")
        assert path is not None
        assert changed is False

    def test_content_change_reports_change(self, tmp_path):
        tc = ServiceTraefikConfig(config_dir=tmp_path)
        tc.write_config(_svc(subdomain="app"), "example.com")
        _, changed = tc.write_config(_svc(subdomain="app2"), "example.com")
        assert changed is True

    def test_unrouted_removes_stale_route(self, tmp_path):
        # A service that WAS routed and no longer is must drop its file —
        # write_config owns that now (rollback_service relies on it).
        tc = ServiceTraefikConfig(config_dir=tmp_path)
        tc.write_config(_svc(routed=True), "example.com")
        path, changed = tc.write_config(_svc(routed=False), "example.com")
        assert path is None and changed is True
        assert not (tmp_path / "app.yaml").exists()

    def test_unrouted_with_no_stale_file_is_a_noop(self, tmp_path):
        tc = ServiceTraefikConfig(config_dir=tmp_path)
        path, changed = tc.write_config(_svc(routed=False), "example.com")
        assert path is None and changed is False


class TestStaticConfigGraceTimeout:
    def test_gracetimeout_present_and_below_stop_timeout(self):
        doc = yaml.safe_load(generate_traefik_static_config())
        for ep in ("web", "websecure"):
            grace = doc["entryPoints"][ep]["transport"]["lifeCycle"]["graceTimeOut"]
            # Strictly below the 30s stop timeout (_reload_traefik +
            # stop_grace_period) or the drain loses the race again.
            assert grace == "20s"


def _mgr(tmp_path):
    from syrviscore.service_manager import ServiceManager

    os.environ.setdefault("DOMAIN", "example.com")
    mgr = ServiceManager(syrvis_home=tmp_path)
    mgr._ensure_directories()
    mgr.reloads = 0

    def _count():
        mgr.reloads += 1

    mgr._reload_traefik = _count
    mgr._start_service = lambda name, cp: (True, "started")
    return mgr


class TestDeployBundleReloadDiscipline:
    def test_unrouted_bundle_never_reloads(self, tmp_path):
        # The deploy-storm case: monitoring-tier services carry no traefik
        # block; deploying them must not touch the edge proxy — fresh or update.
        mgr = _mgr(tmp_path)
        b = {"service": {"name": "exp", "version": "1", "image": "prom/exp:1.0.0"}}
        ok, msg = mgr.deploy_bundle(DeployBundle.from_dict(b))
        assert ok, msg
        assert mgr.reloads == 0
        ok, msg = mgr.deploy_bundle(DeployBundle.from_dict(b))
        assert ok, msg
        assert mgr.reloads == 0

    def test_routed_bundle_reloads_only_on_route_change(self, tmp_path):
        mgr = _mgr(tmp_path)
        m = {
            "name": "app",
            "version": "1",
            "image": "nginx:1.25.0",
            "traefik": {"subdomain": "app", "port": 80},
        }
        ok, msg = mgr.deploy_bundle(DeployBundle.from_dict({"service": m}))
        assert ok, msg
        assert mgr.reloads == 1  # fresh routed install: route is new
        ok, msg = mgr.deploy_bundle(DeployBundle.from_dict({"service": m}))
        assert ok, msg
        assert mgr.reloads == 1  # identical redeploy: no bounce
        m2 = dict(m, traefik={"subdomain": "app2", "port": 80})
        ok, msg = mgr.deploy_bundle(DeployBundle.from_dict({"service": m2}))
        assert ok, msg
        assert mgr.reloads == 2  # real route change: exactly one reload
