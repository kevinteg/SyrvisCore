"""Tests for whole-set convergence (syrvis stack apply --from)."""

import pytest
import yaml

from syrviscore import converge
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


class TestValidateDesired:
    def test_minimal_valid(self):
        out = validate_desired({"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0"}}})
        assert out["on_undeclared"] == "stop"
        assert "wiki" in out["services"]

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

    def test_bad_service_name_rejected(self):
        with pytest.raises(Exception):
            validate_desired({"services": {"../evil": {"image": "a:1"}}})


class TestBuildPlan:
    def test_add_missing_service(self, home):
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=_manager(home))
        kinds = [(a["kind"], a.get("name") or a.get("service")) for a in plan["actions"]]
        assert ("service_add", "wiki") in kinds
        assert plan["changed"] is True
        assert plan["summary"]["destructive"] == 0

    def test_in_sync_is_a_no_op(self, home):
        sm = _manager(home)
        assert sm.add_image("wiki", "ghcr.io/a/wiki:1.0", port=4567, start=False)[0]
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=sm)
        assert plan["changed"] is False
        assert plan["actions"] == []

    def test_changed_image_yields_replace_with_diff(self, home):
        sm = _manager(home)
        assert sm.add_image("wiki", "ghcr.io/a/wiki:1.0", port=4567, start=False)[0]
        desired = validate_desired(
            {"services": {"wiki": {"image": "ghcr.io/a/wiki:2.0", "port": 4567}}}
        )
        plan = build_plan(desired, manager=sm)
        (action,) = plan["actions"]
        assert action["kind"] == "service_replace"
        assert action["changes"]["image"] == {
            "from": "ghcr.io/a/wiki:1.0",
            "to": "ghcr.io/a/wiki:2.0",
        }
        assert action["destructive"] is False

    @pytest.mark.parametrize(
        "policy,kind,destructive",
        [
            ("stop", "service_stop", False),
            ("remove", "service_remove", True),
            ("purge", "service_purge", True),
        ],
    )
    def test_undeclared_service_follows_policy(self, home, policy, kind, destructive):
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        desired = validate_desired({"services": {}, "on_undeclared": policy})
        plan = build_plan(desired, manager=sm)
        (action,) = plan["actions"]
        assert action["kind"] == kind
        assert action["name"] == "old"
        assert action["destructive"] is destructive
        assert plan["summary"]["destructive"] == (1 if destructive else 0)

    def test_stack_enable_disable_diffed(self, home):
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


class _FakeManager:
    """Records apply_plan's dispatch without touching docker/disk."""

    def __init__(self):
        self.calls = []

    def add_image(
        self,
        name,
        image,
        subdomain=None,
        exposure="internal",
        port=80,
        environment=None,
        description="",
        start=True,
        preserve_data_on_rollback=False,
    ):
        self.calls.append(("add_image", name, image, preserve_data_on_rollback))
        return True, "added {}".format(name)

    def remove(self, name, purge=False, keep_declaration=False):
        self.calls.append(("remove", name, purge, keep_declaration))
        return True, "removed {}".format(name)

    def stop(self, name):
        self.calls.append(("stop", name))
        return True, "stopped {}".format(name)


class TestApplyPlan:
    def test_dispatch_and_results(self, home):
        plan = {
            "actions": [
                {
                    "kind": "service_add",
                    "name": "wiki",
                    "image": "a:1",
                    "subdomain": "wiki",
                    "exposure": "internal",
                    "port": 80,
                    "environment": [],
                    "destructive": False,
                },
                {"kind": "service_stop", "name": "old", "destructive": False},
                {"kind": "service_purge", "name": "dead", "destructive": True},
            ]
        }
        fake = _FakeManager()
        results = apply_plan(plan, manager=fake)
        assert [(r["kind"], r["ok"]) for r in results] == [
            ("service_add", True),
            ("service_stop", True),
            ("service_purge", True),
        ]
        assert ("add_image", "wiki", "a:1", False) in fake.calls
        assert ("stop", "old") in fake.calls
        assert ("remove", "dead", True, False) in fake.calls
        assert all(r["changed"] for r in results)

    def test_replace_removes_then_adds(self, home):
        plan = {
            "actions": [
                {
                    "kind": "service_replace",
                    "name": "wiki",
                    "image": "a:2",
                    "subdomain": "wiki",
                    "exposure": "internal",
                    "port": 80,
                    "environment": [],
                    "changes": {},
                    "destructive": False,
                },
            ]
        }
        fake = _FakeManager()
        apply_plan(plan, manager=fake)
        # replace keeps the services.d declaration and preserves pre-existing data
        assert fake.calls == [
            ("remove", "wiki", False, True),
            ("add_image", "wiki", "a:2", True),
        ]

    def test_one_failure_does_not_mask_later_actions(self, home):
        class Flaky(_FakeManager):
            def stop(self, name):
                raise RuntimeError("docker went away")

        plan = {
            "actions": [
                {"kind": "service_stop", "name": "a", "destructive": False},
                {"kind": "service_remove", "name": "b", "destructive": True},
            ]
        }
        results = apply_plan(plan, manager=Flaky())
        assert results[0]["ok"] is False and "docker went away" in results[0]["message"]
        assert results[1]["ok"] is True  # later action still ran


class TestConvergeDryRun:
    def test_dry_run_is_pure(self, home, tmp_path):
        sm = _manager(home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]
        desired_file = tmp_path / "desired.yaml"
        desired_file.write_text(yaml.safe_dump({"services": {}, "on_undeclared": "purge"}))
        plan, results = converge.converge(desired_file, dry_run=True, manager=sm)
        assert results is None
        assert plan["actions"][0]["kind"] == "service_purge"
        # nothing actually happened
        assert (home / "services" / "old").exists()
