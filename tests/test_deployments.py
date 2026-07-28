"""Tests for deployment records, history, and service rollback."""

import json
import subprocess

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore import deployments, services_d
from syrviscore.cli import cli
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import ServiceDefinition


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.setenv("DOMAIN", "example.com")
    monkeypatch.setattr(cli_mod.privilege, "ensure_elevated", lambda reason: None)
    return h


def _manager(home):
    return ServiceManager(syrvis_home=home)


def _definition(name="app", image="ghcr.io/a/app:1.0", **extra):
    doc = {
        "name": name,
        "version": "1.0",
        "image": image,
        "traefik": {"enabled": True, "subdomain": name, "port": 8080, "exposure": "tunnel"},
    }
    doc.update(extra)
    return ServiceDefinition.from_dict(doc)


def _records(home, name):
    d = deployments.workload_dir(home, name)
    return sorted(d.glob("*.json")) if d.exists() else []


class TestRecordSchema:
    def test_service_record_fields(self, home):
        svc = _definition(
            environment=["API_KEY=hunter2", "MODE=fast"],
            volumes=["media:/data/media:ro"],
            env_file="secrets.env",
        )
        svc.source_url = "services.d:app"
        record = deployments.record_service_deploy(
            home, svc, action="deploy", trigger="cli", outcome="success"
        )
        assert record["revision"] == 1
        assert record["schema"] == deployments.SCHEMA
        assert record["env_names"] == ["API_KEY", "MODE"]
        assert record["env_file"] == "secrets.env"
        assert record["image"] == "ghcr.io/a/app:1.0"
        assert record["exposure"] == "tunnel"
        assert record["ports"] == [8080]
        assert record["hostname"] == "app.example.com"
        assert record["source_url"] == "services.d:app"
        assert record["volumes"] == [
            {"source": "media", "target": "/data/media", "mode": "ro", "kind": "service-data"}
        ]
        # The stored manifest keeps full fidelity for rollback.
        assert "API_KEY=hunter2" in record["manifest"]["environment"]

    def test_perms_640_with_inline_env_else_644(self, home):
        (home / "config" / "services.d").mkdir(parents=True, exist_ok=True)
        with_env = _definition(name="withenv", environment=["K=v"])
        deployments.record_service_deploy(
            home, with_env, action="deploy", trigger="cli", outcome="success"
        )
        without = _definition(name="plain")
        deployments.record_service_deploy(
            home, without, action="deploy", trigger="cli", outcome="success"
        )
        mode_env = _records(home, "withenv")[0].stat().st_mode & 0o777
        mode_plain = _records(home, "plain")[0].stat().st_mode & 0o777
        assert mode_env == 0o640
        assert mode_plain == 0o644

    def test_numbering_monotonic_and_collision_safe(self, home):
        svc = _definition()
        deployments.record_service_deploy(
            home, svc, action="deploy", trigger="cli", outcome="success"
        )
        # Simulate a concurrent writer claiming the next number.
        (deployments.workload_dir(home, "app") / "0002.json").write_text("{}")
        record = deployments.record_service_deploy(
            home, svc, action="deploy", trigger="cli", outcome="success"
        )
        assert record["revision"] == 3
        names = [p.name for p in _records(home, "app")]
        assert names == ["0001.json", "0002.json", "0003.json"]

    def test_auto_trim_keeps_newest(self, home, monkeypatch):
        monkeypatch.setattr(deployments, "DEFAULT_KEEP", 3)
        svc = _definition()
        for _ in range(5):
            deployments.record_service_deploy(
                home, svc, action="deploy", trigger="cli", outcome="success"
            )
        names = [p.name for p in _records(home, "app")]
        assert names == ["0003.json", "0004.json", "0005.json"]

    def test_workload_containment(self, home):
        with pytest.raises(Exception):
            deployments.workload_dir(home, "../evil")
        with pytest.raises(Exception):
            deployments.workload_dir(home, "@nope")
        assert deployments.workload_dir(home, "@core").name == "@core"

    def test_recording_is_best_effort(self, home, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(deployments, "_write_record", boom)
        assert (
            deployments.record_service_deploy(
                home, _definition(), action="deploy", trigger="cli", outcome="success"
            )
            is None
        )
        # ...and a real verb still succeeds with recording broken.
        sm = _manager(home)
        ok, msg = sm.add_image("quiet", "ghcr.io/a/quiet:1.0", start=False)
        assert ok, msg


class TestVerbRecording:
    def test_add_image_records_deploy(self, home):
        sm = _manager(home)
        assert sm.add_image("web", "ghcr.io/a/web:1.0", start=False)[0]
        history = deployments.load_history(home, "web")
        (record,) = history["workloads"]["web"]
        assert (record["action"], record["trigger"], record["outcome"]) == (
            "deploy",
            "cli",
            "success",
        )
        assert record["previous_image"] is None

    def test_reconcile_replace_records_one_deploy_with_transition(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("web", "ghcr.io/a/web:1.0", start=False)[0]
        base = len(_records(home, "web"))
        # Declare a newer image; reconcile plans a replace.
        decl_path = services_d.declaration_path(home, "web")
        decl = yaml.safe_load(decl_path.read_text())
        decl["image"] = "ghcr.io/a/web:2.0"
        decl_path.write_text(yaml.safe_dump(decl))
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c: (True, "ok"))

        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert [a["kind"] for a in plan["actions"]] == ["replace"]
        services_d.apply_reconcile_plan(sm, declarations, plan)

        records = _records(home, "web")
        assert len(records) == base + 1  # ONE record for the replace, not remove+add
        record = json.loads(records[-1].read_text())
        assert record["action"] == "deploy"
        assert record["trigger"] == "reconcile"
        assert record["previous_image"] == "ghcr.io/a/web:1.0"
        assert record["image"] == "ghcr.io/a/web:2.0"

    def test_remove_records_and_replace_teardown_is_silent(self, home):
        sm = _manager(home)
        assert sm.add_image("gone", "ghcr.io/a/gone:1.0", start=False)[0]
        base = len(_records(home, "gone"))
        # reconcile-replace teardown: silent
        assert sm.remove("gone", keep_declaration=True)[0]
        assert len(_records(home, "gone")) == base
        # reinstall + imperative remove: recorded
        assert sm.install_declaration(
            _definition(name="gone", image="ghcr.io/a/gone:1.0"), start=False
        )[0]
        assert sm.remove("gone")[0]
        record = json.loads(_records(home, "gone")[-1].read_text())
        assert record["action"] == "remove"
        assert record["previous_image"] == "ghcr.io/a/gone:1.0"

    def test_set_image_records_transition(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("api", "ghcr.io/a/api:1.0", start=False)[0]
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        )
        ok, msg = sm.set_image("api", "ghcr.io/a/api:2.0")
        assert ok, msg
        record = json.loads(_records(home, "api")[-1].read_text())
        assert record["previous_image"] == "ghcr.io/a/api:1.0"
        assert record["image"] == "ghcr.io/a/api:2.0"

    def test_set_image_pull_failure_records_nothing(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("api", "ghcr.io/a/api:1.0", start=False)[0]
        base = len(_records(home, "api"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such tag")
        )
        ok, msg = sm.set_image("api", "ghcr.io/a/api:9.9")
        assert not ok
        assert len(_records(home, "api")) == base  # unchanged state -> no record


class TestHistoryCli:
    def test_history_json_redacts_env_values(self, home):
        svc = _definition(environment=["SECRET_TOKEN=xyz", "PLAIN=1"])
        deployments.record_service_deploy(
            home, svc, action="deploy", trigger="cli", outcome="success"
        )
        result = CliRunner().invoke(cli, ["history", "app", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        (record,) = body["workloads"]["app"]
        assert record["env_names"] == ["SECRET_TOKEN", "PLAIN"]
        assert record["manifest"]["environment"] == ["SECRET_TOKEN=****", "PLAIN=****"]
        assert "xyz" not in result.output
        # The raw record (rollback source) still holds the value.
        raw = deployments.load_revision(home, "app", 1)
        assert "SECRET_TOKEN=xyz" in raw["manifest"]["environment"]

    def test_history_human_table_and_empty(self, home):
        result = CliRunner().invoke(cli, ["history"])
        assert result.exit_code == 0
        assert "No deployment history" in result.output

        deployments.record_service_deploy(
            home, _definition(), action="deploy", trigger="cli", outcome="success"
        )
        result = CliRunner().invoke(cli, ["history"])
        assert result.exit_code == 0
        assert "app" in result.output and "deploy" in result.output

    def test_history_corrupt_record_isolated(self, home):
        deployments.record_service_deploy(
            home, _definition(), action="deploy", trigger="cli", outcome="success"
        )
        (deployments.workload_dir(home, "app") / "0002.json").write_text("{broken")
        report = deployments.load_history(home)
        assert len(report["workloads"]["app"]) == 1
        assert report["invalid"] and "0002.json" in report["invalid"][0]["file"]


class TestCoreRecord:
    def test_record_core_apply_roundtrip(self, home):
        pins = {"traefik": "traefik:v3.6.5", "portainer": "portainer/portainer-ce:2.43.0"}
        record = deployments.record_core_apply(
            home, pins=pins, core_enabled=["portainer", "traefik"], trigger="cli"
        )
        assert record["workload"] == "@core"
        assert record["tier"] == "core"
        latest = deployments.latest_revision(home, deployments.CORE_WORKLOAD)
        assert latest["pins"] == pins
        # '@core' shows up in unfiltered history
        report = deployments.load_history(home)
        assert "@core" in report["workloads"]


class TestRollback:
    def _deploy_twice(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("blog", "ghcr.io/a/blog:1.0", start=False)[0]
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        )
        assert sm.set_image("blog", "ghcr.io/a/blog:2.0")[0]
        return sm

    def test_rollback_restores_previous_revision(self, home, monkeypatch):
        sm = self._deploy_twice(home, monkeypatch)
        ok, msg = sm.rollback("blog")
        assert ok, msg
        manifest = yaml.safe_load((home / "services" / "blog" / "syrvis-service.yaml").read_text())
        assert manifest["image"] == "ghcr.io/a/blog:1.0"
        # declaration content follows (dual-write)
        decl = yaml.safe_load(services_d.declaration_path(home, "blog").read_text())
        assert decl["image"] == "ghcr.io/a/blog:1.0"
        record = json.loads(_records(home, "blog")[-1].read_text())
        assert record["action"] == "rollback"
        assert record["rollback_of"] == 1
        assert record["previous_image"] == "ghcr.io/a/blog:2.0"

    def test_rollback_to_explicit_revision(self, home, monkeypatch):
        sm = self._deploy_twice(home, monkeypatch)
        ok, msg = sm.rollback("blog", to=1)
        assert ok, msg
        assert "revision 1" in msg

    def test_rollback_unpullable_target_aborts(self, home, monkeypatch):
        sm = self._deploy_twice(home, monkeypatch)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "gone")
        )
        ok, msg = sm.rollback("blog")
        assert not ok and "left unchanged" in msg
        manifest = yaml.safe_load((home / "services" / "blog" / "syrvis-service.yaml").read_text())
        assert manifest["image"] == "ghcr.io/a/blog:2.0"  # untouched
        record = json.loads(_records(home, "blog")[-1].read_text())
        assert (record["action"], record["outcome"]) == ("rollback", "failed")

    def test_rollback_refuses_git_service(self, home, monkeypatch):
        sm = self._deploy_twice(home, monkeypatch)
        (home / "services" / "blog" / ".git").mkdir()
        ok, msg = sm.rollback("blog")
        assert not ok and "git" in msg

    def test_rollback_without_history_errors(self, home):
        sm = _manager(home)
        assert sm.add_image("new", "ghcr.io/a/new:1.0", start=False)[0]
        ok, msg = sm.rollback("new")
        assert not ok and "no earlier successful revision" in msg

    def test_rollback_preserves_current_orchestration(self, home, monkeypatch):
        sm = self._deploy_twice(home, monkeypatch)
        services_d.set_declared_enabled(home, "blog", False)  # operator turned it off
        ok, msg = sm.rollback("blog")
        assert ok, msg
        decl = yaml.safe_load(services_d.declaration_path(home, "blog").read_text())
        assert decl["enabled"] is False  # rollback restores CONTENT, not orchestration

    def test_rollback_cli_wiring(self, home, monkeypatch):
        recorded = {}

        def fake_rollback(self, name, to=None, **kwargs):
            recorded["args"] = (name, to)
            return True, "rolled back"

        monkeypatch.setattr(ServiceManager, "rollback", fake_rollback)
        result = CliRunner().invoke(cli, ["service", "rollback", "blog", "--to", "3", "-y"])
        assert result.exit_code == 0, result.output
        assert recorded["args"] == ("blog", 3)


class TestResolveTarget:
    def test_skips_failed_and_remove_records(self, home):
        svc1 = _definition(image="ghcr.io/a/app:1.0")
        svc2 = _definition(image="ghcr.io/a/app:2.0")
        deployments.record_service_deploy(
            home, svc1, action="deploy", trigger="cli", outcome="success"
        )
        deployments.record_service_deploy(
            home, svc2, action="deploy", trigger="cli", outcome="failed"
        )
        deployments.record_service_deploy(
            home, svc2, action="deploy", trigger="cli", outcome="success"
        )
        target = deployments.resolve_rollback_target(home, "app")
        assert target["revision"] == 1  # rev 2 failed -> skipped

        with pytest.raises(deployments.DeploymentError):
            deployments.resolve_rollback_target(home, "app", to=2)  # failed record

    def test_seam_registry_has_new_commands(self):
        from syrviscore.seam import registry

        history_cmd = registry.COMMANDS_BY_ID["deployment_history"]
        assert history_cmd.read_only and not history_cmd.sudo
        rollback_cmd = registry.COMMANDS_BY_ID["service_rollback"]
        assert rollback_cmd.sudo and rollback_cmd.destructive
        assert rollback_cmd.subcommand == ["service", "rollback"]


class TestReviewRegressions:
    def test_reserved_platform_dirs_rejected_as_service_names(self, home):
        sm = _manager(home)
        for name in ("deployments", "state"):
            ok, msg = sm.add_image(name, "ghcr.io/a/x:1.0", start=False)
            assert not ok and "reserved" in msg.lower()

    def test_failed_reconcile_replace_records_failed_deploy(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("web", "ghcr.io/a/web:1.0", start=False)[0]
        base = len(_records(home, "web"))
        decl_path = services_d.declaration_path(home, "web")
        decl = yaml.safe_load(decl_path.read_text())
        decl["image"] = "ghcr.io/a/web:2.0"
        decl_path.write_text(yaml.safe_dump(decl))
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c, **kw: (True, "ok"))
        monkeypatch.setattr(
            ServiceManager, "_start_service", lambda self, n, c: (False, "unpullable")
        )
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        services_d.apply_reconcile_plan(sm, declarations, plan)
        record = json.loads(_records(home, "web")[-1].read_text())
        assert len(_records(home, "web")) == base + 1
        assert (record["action"], record["outcome"]) == ("deploy", "failed")
        assert record["previous_image"] == "ghcr.io/a/web:1.0"

    def test_rollback_refuses_stolen_hostname(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("blog", "ghcr.io/a/blog:1.0", subdomain="shared", start=False)[0]
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c, **kw: (True, "ok"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        )
        assert sm.set_image("blog", "ghcr.io/a/blog:2.0")[0]
        # Move blog off the subdomain, give it to another service.
        bundle_doc = yaml.safe_load(
            (home / "services" / "blog" / "syrvis-service.yaml").read_text()
        )
        bundle_doc["traefik"]["subdomain"] = "elsewhere"
        from syrviscore.service_schema import ServiceDefinition as SD

        sm._write_manifest(SD.from_dict(bundle_doc), home / "services" / "blog")
        assert sm.add_image("thief", "ghcr.io/a/thief:1.0", subdomain="shared", start=False)[0]
        ok, msg = sm.rollback("blog", to=1)  # revision 1 routed 'shared'
        assert not ok and "owned by service 'thief'" in msg

    def test_rollback_to_unrouted_revision_removes_route(self, home, monkeypatch):
        sm = _manager(home)
        unrouted = ServiceDefinition.from_dict(
            {"name": "job", "version": "1", "image": "ghcr.io/a/job:1.0"}
        )
        assert sm.install_declaration(unrouted, start=False)[0]
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c, **kw: (True, "ok"))
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        )
        routed = unrouted.to_dict()
        routed["image"] = "ghcr.io/a/job:2.0"
        routed["traefik"] = {"enabled": True, "subdomain": "job", "port": 80}
        sm.remove("job", keep_declaration=True)
        assert sm.install_declaration(
            ServiceDefinition.from_dict(routed), start=False, previous_image="ghcr.io/a/job:1.0"
        )[0]
        route = home / "data" / "traefik" / "config" / "dynamic" / "job.yaml"
        assert route.exists()
        ok, msg = sm.rollback("job", to=1)  # revision 1 was unrouted
        assert ok, msg
        assert not route.exists()

    def test_pre_deploy_abort_preserves_preexisting_data(self, home, monkeypatch):
        sm = _manager(home)
        keepsake = home / "data" / "photos" / "library.db"
        keepsake.parent.mkdir(parents=True)
        keepsake.write_text("precious")
        monkeypatch.setattr(
            ServiceManager,
            "_fire_hooks",
            lambda self, target, event, **kw: (
                (False, []) if event == "pre-deploy" else (True, [])
            ),
        )
        ok, msg = sm.add_image("photos", "ghcr.io/a/photos:1.0", start=False)
        assert not ok and "pre-deploy hook" in msg
        assert keepsake.exists()  # abort must never destroy pre-existing data

    def test_records_beyond_9999_stay_visible(self, home):
        svc = _definition(name="veteran")
        d = deployments.workload_dir(home, "veteran")
        d.mkdir(parents=True)
        (d / "9999.json").write_text(
            json.dumps({"schema": deployments.SCHEMA, "revision": 9999, "workload": "veteran"})
        )
        record = deployments.record_service_deploy(
            home, svc, action="deploy", trigger="cli", outcome="success"
        )
        assert record["revision"] == 10000
        latest = deployments.latest_revision(home, "veteran")
        assert latest["revision"] == 10000
        assert deployments.next_revision(d) == 10001
