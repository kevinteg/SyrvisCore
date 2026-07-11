"""Tests for whole-set convergence (`stack apply --from`), unified onto the
services.d engine: the doc's `services:` section is a projection that syncs
declarations and then runs the same reconcile planner as `syrvis reconcile`."""

import pytest
import yaml

from syrviscore import converge, services_d
from syrviscore.converge import ConvergeError, apply_plan, build_plan, validate_desired
from syrviscore.service_manager import ServiceManager


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    (h / "config" / "stack.yaml").write_text(
        "version: 1\n"
        "services:\n"
        "  traefik: {enabled: true}\n"
        "  portainer: {enabled: true}\n"
        "  cloudflared: {enabled: true}\n"
        "  dashboard: {enabled: false}\n"
        "  cloudflare_ddns: {enabled: false}\n"
    )
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.setenv("DOMAIN", "example.com")
    return h


def _manager(home):
    return ServiceManager(syrvis_home=home)


def _running(monkeypatch, status="running"):
    monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: status)


class TestValidateDesired:
    def test_minimal_valid(self):
        out = validate_desired({"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0"}}})
        assert out["on_undeclared"] == "stop"
        assert out["manages_services"] is True
        assert "wiki" in out["services"]

    def test_absent_services_key_does_not_manage_l2(self):
        out = validate_desired({"stack": {"dashboard": {"enabled": True}}})
        assert out["manages_services"] is False

    def test_empty_services_mapping_still_manages_l2(self):
        assert validate_desired({"services": {}})["manages_services"] is True

    def test_orchestration_keys_accepted_in_doc(self):
        out = validate_desired(
            {"services": {"wiki": {"image": "a/b:1.0", "critical": True, "enabled": False}}}
        )
        assert out["services"]["wiki"]["critical"] is True

    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(ConvergeError, match="Unknown keys"):
            validate_desired({"servcies": {}})

    def test_unknown_service_key_rejected(self):
        with pytest.raises(ConvergeError, match="unknown keys"):
            validate_desired({"services": {"wiki": {"image": "a:1", "privileged": True}}})

    def test_primordial_disable_rejected(self):
        with pytest.raises(ConvergeError, match="primordial"):
            validate_desired({"stack": {"traefik": {"enabled": False}}})

    def test_unknown_core_service_rejected(self):
        with pytest.raises(ConvergeError, match="Unknown core service"):
            validate_desired({"stack": {"nginx": {"enabled": True}}})

    def test_bad_on_undeclared_rejected(self):
        with pytest.raises(ConvergeError, match="on_undeclared"):
            validate_desired({"on_undeclared": "explode"})

    def test_image_required(self):
        with pytest.raises(ConvergeError, match="'image' is required"):
            validate_desired({"services": {"wiki": {"subdomain": "wiki"}}})


class TestBuildPlan:
    def test_missing_service_yields_declare_plus_add(self, home, monkeypatch):
        _running(monkeypatch, "unknown")
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=_manager(home))
        kinds = [(a["kind"], a.get("name")) for a in plan["actions"]]
        assert kinds == [("declare", "wiki"), ("add", "wiki")]
        assert plan["manages_services"] is True
        assert "wiki" in plan["declarations"]
        assert plan["summary"]["destructive"] == 0

    def test_in_sync_is_a_no_op(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("wiki", "ghcr.io/a/wiki:1.0", port=4567, start=False)[0]
        _running(monkeypatch)  # installed, declaration dual-written, running
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=sm)
        assert plan["actions"] == []
        assert plan["changed"] is False

    def test_changed_image_yields_declare_update_plus_replace(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("wiki", "ghcr.io/a/wiki:1.0", port=4567, start=False)[0]
        _running(monkeypatch)
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:2.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=sm)
        kinds = [a["kind"] for a in plan["actions"]]
        assert kinds == ["declare_update", "replace"]

    def test_doc_without_services_key_never_touches_l2(self, home, monkeypatch):
        """The phase-1 review footgun, closed: a core-stack-only doc leaves
        installed services and their declarations completely alone."""
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        _running(monkeypatch)
        desired = validate_desired({"stack": {"dashboard": {"enabled": True}}})
        plan = build_plan(desired, manager=sm)
        kinds = {a["kind"] for a in plan["actions"]}
        assert kinds == {"stack_enable"}
        assert plan["manages_services"] is False

    @pytest.mark.parametrize(
        "policy,sync_kind,engine_kind,destructive",
        [
            ("stop", "declare_disable", "stop", False),
            ("remove", "declare_delete", "prune_remove", True),
            ("purge", "declare_delete", "prune_purge", True),
        ],
    )
    def test_undeclared_service_follows_policy(
        self, home, monkeypatch, policy, sync_kind, engine_kind, destructive
    ):
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        _running(monkeypatch)
        desired = validate_desired({"services": {}, "on_undeclared": policy})
        plan = build_plan(desired, manager=sm)
        kinds = [(a["kind"], a.get("name")) for a in plan["actions"]]
        assert (sync_kind, "old") in kinds
        assert (engine_kind, "old") in kinds
        assert plan["summary"]["destructive"] == (1 if destructive else 0)

    def test_stack_enable_disable_diffed(self, home, monkeypatch):
        _running(monkeypatch)
        desired = validate_desired(
            {
                "stack": {
                    "dashboard": {"enabled": True, "subdomain": "dash"},  # currently off
                    "cloudflared": {"enabled": False},  # currently on
                    "portainer": {"enabled": True},  # already on -> no action
                }
            }
        )
        plan = build_plan(desired, manager=_manager(home))
        kinds = {(a["kind"], a["service"]) for a in plan["actions"]}
        assert ("stack_enable", "dashboard") in kinds
        assert ("stack_disable", "cloudflared") in kinds
        assert not any(a.get("service") == "portainer" for a in plan["actions"])


class TestApplyPlan:
    """Integration-style: real home + declarations, docker faked at the edges."""

    def test_apply_syncs_declarations_and_runs_the_engine(self, home, monkeypatch):
        _running(monkeypatch, "unknown")
        installed = []
        monkeypatch.setattr(
            ServiceManager,
            "install_declaration",
            lambda self, service, start=True, preserve_data_on_rollback=False: (
                installed.append((service.name, service.image)) or (True, "installed")
            ),
        )
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "critical": True}}}
        )
        sm = _manager(home)
        plan = build_plan(desired, manager=sm)
        results = apply_plan(plan, manager=sm)

        assert [(r["kind"], r["ok"]) for r in results] == [("declare", True), ("add", True)]
        assert installed == [("wiki", "ghcr.io/a/wiki:1.0")]
        # the declaration landed with the doc's orchestration intact
        decl = yaml.safe_load(services_d.declaration_path(home, "wiki").read_text())
        assert decl["critical"] is True

    def test_apply_purge_policy_deletes_declaration_and_prunes(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        _running(monkeypatch)
        removed = []
        monkeypatch.setattr(
            ServiceManager,
            "remove",
            lambda self, name, purge=False, keep_declaration=False: (
                removed.append((name, purge)) or (True, "removed")
            ),
        )
        desired = validate_desired({"services": {}, "on_undeclared": "purge"})
        plan = build_plan(desired, manager=sm)
        results = apply_plan(plan, manager=sm)

        assert not services_d.declaration_path(home, "old").exists()
        assert ("old", True) in removed  # prune_purge via the engine
        assert all(r["ok"] for r in results)

    def test_one_failure_does_not_mask_later_actions(self, home, monkeypatch):
        _running(monkeypatch, "unknown")

        def flaky_install(self, service, start=True, preserve_data_on_rollback=False):
            if service.name == "flaky":
                raise RuntimeError("docker went away")
            return True, "installed"

        monkeypatch.setattr(ServiceManager, "install_declaration", flaky_install)
        desired = validate_desired(
            {
                "services": {
                    "flaky": {"image": "ghcr.io/a/flaky:1.0"},
                    "steady": {"image": "ghcr.io/a/steady:1.0"},
                }
            }
        )
        sm = _manager(home)
        plan = build_plan(desired, manager=sm)
        results = apply_plan(plan, manager=sm)
        by_name = {(r["kind"], r["name"]): r["ok"] for r in results}
        assert by_name[("add", "flaky")] is False
        assert by_name[("add", "steady")] is True


class TestConvergeDryRun:
    def test_dry_run_is_pure(self, home, tmp_path, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        _running(monkeypatch)
        desired_file = tmp_path / "desired.yaml"
        desired_file.write_text(yaml.safe_dump({"services": {}, "on_undeclared": "purge"}))
        plan, results = converge.converge(desired_file, dry_run=True, manager=sm)
        assert results is None
        kinds = {a["kind"] for a in plan["actions"]}
        assert kinds == {"declare_delete", "prune_purge"}
        # nothing actually happened: install AND declaration both intact
        assert (home / "services" / "old").exists()
        assert services_d.declaration_path(home, "old").exists()
