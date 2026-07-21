"""Tests for declarative service loading (config/services.d + syrvis reconcile)."""

import json

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore import services_d
from syrviscore.cli import cli
from syrviscore.service_manager import ServiceManager


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


def _declare(home, name, image="ghcr.io/a/{}:1.0", **extra):
    d = services_d.get_declarations_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "version": "1.0",
        "image": image.format(name),
        "traefik": {"enabled": True, "subdomain": name, "port": 80, "exposure": "internal"},
    }
    doc.update(extra)
    (d / "{}.yaml".format(name)).write_text(yaml.safe_dump(doc))
    return d / "{}.yaml".format(name)


class TestLoadIsolation:
    def test_broken_file_never_blocks_the_others(self, home):
        _declare(home, "good-one")
        _declare(home, "good-two")
        d = services_d.get_declarations_dir(home)
        (d / "broken.yaml").write_text("{not yaml: [")
        (d / "latest.yaml").write_text(
            yaml.safe_dump({"name": "latest", "version": "1", "image": "nginx:latest"})
        )

        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"good-one", "good-two"}
        assert {row["file"] for row in invalid} == {"broken.yaml", "latest.yaml"}
        assert all(row["error"] for row in invalid)

    def test_filename_must_match_name(self, home):
        d = services_d.get_declarations_dir(home)
        d.mkdir(parents=True)
        (d / "impostor.yaml").write_text(
            yaml.safe_dump({"name": "other", "version": "1", "image": "a/b:1.0"})
        )
        valid, invalid = services_d.load_declarations(home)
        assert not valid
        assert "must match its filename" in invalid[0]["error"]

    def test_missing_dir_is_empty_not_error(self, home):
        assert services_d.load_declarations(home) == ({}, [])


class TestPlan:
    def test_add_start_replace_in_sync(self, home, monkeypatch):
        sm = _manager(home)
        # installed + matching content
        assert sm.add_image("synced", "ghcr.io/a/synced:1.0", start=False)[0]
        services_d.adopt(sm, "synced")
        # installed but declaration differs (new image)
        assert sm.add_image("drifted", "ghcr.io/a/drifted:1.0", start=False)[0]
        services_d.adopt(sm, "drifted")
        decl = yaml.safe_load(services_d.declaration_path(home, "drifted").read_text())
        decl["image"] = "ghcr.io/a/drifted:2.0"
        services_d.declaration_path(home, "drifted").write_text(yaml.safe_dump(decl))
        # declared but not installed
        _declare(home, "missing")

        statuses = {"synced": "running", "drifted": "running", "missing": "unknown"}
        monkeypatch.setattr(
            ServiceManager, "_get_service_status", lambda self, name: statuses.get(name, "unknown")
        )

        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        kinds = {a["name"]: a["kind"] for a in plan["actions"]}
        assert kinds == {"drifted": "replace", "missing": "add"}
        assert plan["in_sync"] == ["synced"]

    def test_stopped_but_matching_gets_start(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.adopt(sm, "app")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "stopped")
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert [a["kind"] for a in plan["actions"]] == ["start"]

    def test_declared_disabled_stops_running_service(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.adopt(sm, "app")
        services_d.set_declared_enabled(home, "app", False)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert [a["kind"] for a in plan["actions"]] == ["stop"]

        # ...and once stopped it's just "disabled", not an action.
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "stopped")
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert plan["actions"] == []
        assert plan["disabled"] == ["app"]

    def test_orchestration_only_change_never_replaces(self, home, monkeypatch):
        """Flipping `critical` must not restart the container."""
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.adopt(sm, "app")
        decl_path = services_d.declaration_path(home, "app")
        decl = yaml.safe_load(decl_path.read_text())
        decl["critical"] = True
        decl_path.write_text(yaml.safe_dump(decl))

        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert plan["actions"] == []
        assert plan["in_sync"] == ["app"]

    def test_unmanaged_reported_never_touched_without_prune(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("legacy", "ghcr.io/a/legacy:1.0", start=False)[0]
        services_d.remove_declaration(home, "legacy")  # dual-write undone -> unmanaged
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        plan = services_d.build_reconcile_plan(sm, {}, [])
        assert plan["unmanaged"] == ["legacy"]
        assert plan["actions"] == []

    @pytest.mark.parametrize(
        "policy,kind,destructive",
        [
            ("stop", "prune_stop", False),
            ("remove", "prune_remove", True),
            ("purge", "prune_purge", True),
        ],
    )
    def test_prune_policies(self, home, monkeypatch, policy, kind, destructive):
        sm = _manager(home)
        assert sm.add_image("legacy", "ghcr.io/a/legacy:1.0", start=False)[0]
        services_d.remove_declaration(home, "legacy")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
        plan = services_d.build_reconcile_plan(sm, {}, [], prune=policy)
        (action,) = plan["actions"]
        assert (action["kind"], action["destructive"]) == (kind, destructive)


class _FakeManager:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def install_declaration(self, service, start=True, preserve_data_on_rollback=False):
        self.calls.append(("install", service.name, preserve_data_on_rollback))
        if service.name in self.fail:
            return False, "boom"
        return True, "installed"

    def remove(self, name, purge=False, keep_declaration=False):
        self.calls.append(("remove", name, purge, keep_declaration))
        return True, "removed"

    def start(self, name):
        self.calls.append(("start", name))
        if name in self.fail:
            raise RuntimeError("docker exploded")
        return True, "started"

    def stop(self, name):
        self.calls.append(("stop", name))
        return True, "stopped"


class TestApplyIsolation:
    def _decl(self, name, critical=False):
        from syrviscore.service_schema import ServiceDefinition

        return ServiceDefinition.from_dict(
            {
                "name": name,
                "version": "1",
                "image": "ghcr.io/a/{}:1.0".format(name),
                "critical": critical,
            }
        )

    def test_one_failure_never_blocks_the_rest(self):
        fake = _FakeManager(fail={"flaky"})
        declarations = {n: self._decl(n) for n in ("flaky", "steady")}
        plan = {
            "actions": [
                {"kind": "start", "name": "flaky", "critical": False, "destructive": False},
                {"kind": "add", "name": "steady", "critical": False, "destructive": False},
            ]
        }
        results = services_d.apply_reconcile_plan(fake, declarations, plan)
        assert [(r["name"], r["ok"]) for r in results] == [("flaky", False), ("steady", True)]
        assert "docker exploded" in results[0]["message"]

    def test_replace_keeps_declaration(self):
        fake = _FakeManager()
        declarations = {"app": self._decl("app")}
        plan = {
            "actions": [{"kind": "replace", "name": "app", "critical": False, "destructive": False}]
        }
        services_d.apply_reconcile_plan(fake, declarations, plan)
        assert ("remove", "app", False, True) in fake.calls  # keep_declaration=True
        # ...and the pre-existing data dir survives a failed re-install
        assert ("install", "app", True) in fake.calls

    def test_verdict_critical_vs_not(self):
        plan = {"invalid": []}
        noncrit = [{"name": "a", "ok": False, "critical": False, "kind": "start", "message": "x"}]
        crit = [{"name": "b", "ok": False, "critical": True, "kind": "start", "message": "x"}]
        assert services_d.verdict(plan, noncrit)[0] is True  # degraded, not fatal
        assert services_d.verdict(plan, crit)[0] is False
        assert services_d.verdict(plan, noncrit, strict=True)[0] is False
        assert (
            services_d.verdict({"invalid": [{"file": "x", "error": "y"}]}, [], strict=True)[0]
            is False
        )


class TestDualWrite:
    def test_add_image_writes_declaration(self, home):
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        path = services_d.declaration_path(home, "app")
        assert path.exists()
        assert yaml.safe_load(path.read_text())["image"] == "ghcr.io/a/app:1.0"

    def test_remove_deletes_declaration(self, home):
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        assert services_d.declaration_path(home, "app").exists()
        assert sm.remove("app")[0]
        assert not services_d.declaration_path(home, "app").exists()

    def test_adopt_generates_declaration(self, home):
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.remove_declaration(home, "app")
        path = services_d.adopt(sm, "app")
        assert path.exists()
        assert yaml.safe_load(path.read_text())["name"] == "app"

    def test_declaration_with_secrets_is_0600(self, home):
        import stat

        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", environment=["TOKEN=s3cret"], start=False)[
            0
        ]
        path = services_d.declaration_path(home, "app")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestReconcileCli:
    def test_dry_run_json(self, home, monkeypatch):
        _declare(home, "app")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "unknown")
        result = CliRunner().invoke(cli, ["reconcile", "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["applied"] is False
        assert body["plan"]["actions"][0]["kind"] == "add"

    def test_invalid_file_is_fatal_but_isolated(self, home, monkeypatch):
        """A corrupted declaration must fail the run (intent corruption never
        passes silently) while every OTHER service still converges."""
        d = services_d.get_declarations_dir(home)
        d.mkdir(parents=True)
        (d / "broken.yaml").write_text("{not yaml: [")
        _declare(home, "healthy")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "unknown")
        monkeypatch.setattr(
            ServiceManager,
            "install_declaration",
            lambda self, service, start=True, preserve_data_on_rollback=False: (True, "installed"),
        )
        result = CliRunner().invoke(cli, ["reconcile", "--json"])
        assert result.exit_code == 1, result.output
        body = json.loads(result.output)
        assert body["ok"] is False
        assert body["plan"]["summary"]["invalid"] == 1
        # isolation: the healthy service was still converged
        assert [(r["name"], r["ok"]) for r in body["results"]] == [("healthy", True)]

        # --boot demotes even this to best-effort exit 0
        result = CliRunner().invoke(cli, ["reconcile", "--json", "--boot"])
        assert result.exit_code == 0

    def test_prune_requires_confirmation(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("legacy", "ghcr.io/a/legacy:1.0", start=False)[0]
        services_d.remove_declaration(home, "legacy")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")

        result = CliRunner().invoke(cli, ["reconcile", "--prune", "purge"], input="n\n")
        assert result.exit_code != 0
        assert (home / "services" / "legacy").exists()  # nothing pruned

    def test_boot_mode_always_exits_zero_and_never_prunes(self, home, monkeypatch):
        sm = _manager(home)
        assert sm.add_image("legacy", "ghcr.io/a/legacy:1.0", start=False)[0]
        services_d.remove_declaration(home, "legacy")
        # a critical declared service that will fail to converge
        _declare(home, "vital", critical=True)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "unknown")
        monkeypatch.setattr(
            ServiceManager,
            "install_declaration",
            lambda self, service, start=True: (False, "no docker at boot-test time"),
        )
        result = CliRunner().invoke(cli, ["reconcile", "--boot", "--prune", "purge", "--json"])
        assert result.exit_code == 0, result.output  # best-effort: never fatal
        body = json.loads(result.output)
        assert body["ok"] is False  # honestly reported...
        assert (home / "services" / "legacy").exists()  # ...and nothing pruned

    def test_service_adopt_cli(self, home, monkeypatch):
        sm_home = home
        sm = _manager(sm_home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.remove_declaration(sm_home, "app")
        result = CliRunner().invoke(cli, ["service", "adopt", "app"])
        assert result.exit_code == 0, result.output
        assert services_d.declaration_path(sm_home, "app").exists()


class TestOrchestrationBoundary:
    """Orchestration keys live ONLY in declarations; manifests stay clean."""

    def test_manifest_strips_orchestration_but_declaration_keeps_it(self, home, monkeypatch):
        from syrviscore.service_schema import ServiceDefinition

        declared = ServiceDefinition.from_dict(
            {
                "name": "vital",
                "version": "1",
                "image": "ghcr.io/a/vital:1.0",
                "critical": True,
                "enabled": True,
            }
        )
        sm = _manager(home)
        ok, msg = sm.install_declaration(declared, start=False)
        assert ok, msg

        manifest = yaml.safe_load((home / "services" / "vital" / "syrvis-service.yaml").read_text())
        assert "critical" not in manifest  # older versions must keep parsing this
        assert "enabled" not in manifest

        declaration = yaml.safe_load(services_d.declaration_path(home, "vital").read_text())
        assert declaration["critical"] is True

    def test_dual_write_preserves_operator_orchestration(self, home):
        """An imperative install can never reset the operator's enabled/critical."""
        from syrviscore.service_schema import ServiceDefinition

        # Operator-authored declaration (critical) exists before the install.
        operator = ServiceDefinition.from_dict(
            {"name": "app", "version": "1", "image": "ghcr.io/a/app:1.0", "critical": True}
        )
        services_d.write_declaration(home, operator)

        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:2.0", start=False)[0]

        declaration = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert declaration["image"] == "ghcr.io/a/app:2.0"  # content updated...
        assert declaration["critical"] is True  # ...operator intent preserved

    def test_declaration_write_failure_never_rolls_back_the_install(self, home, monkeypatch):
        """The dual-write is outside the rollback boundary: a services.d write
        failure must not tear down a freshly working service."""
        monkeypatch.setattr(
            services_d,
            "write_declaration_from_install",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
        )
        sm = _manager(home)
        ok, msg = sm.add_image("app", "ghcr.io/a/app:1.0", start=False)
        assert ok
        assert "could not write services.d declaration" in msg
        assert (home / "services" / "app").exists()  # install intact
        assert (home / "compose" / "app.yaml").exists()

    def test_manifest_less_dir_is_planned_as_replace_not_add(self, home, monkeypatch):
        """A crash mid-install leaves services/<name>/ without a manifest; the
        declaration must plan a REPLACE (which clears the wreck) — an ADD would
        refuse on the existing directory forever."""
        (home / "services" / "app").mkdir(parents=True)  # wreckage, no manifest
        _declare(home, "app")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "unknown")
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(_manager(home), declarations, invalid)
        assert [a["kind"] for a in plan["actions"]] == ["replace"]

    def test_reconcile_start_does_not_rewrite_declaration(self, home, monkeypatch):
        """A no-op enabled flip must not churn (re-serialize/re-own) the
        IaC-authored file — reconcile must not mutate its own source of truth."""
        sm = _manager(home)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        path = services_d.declaration_path(home, "app")
        before = path.read_text()
        sentinel = before + "# operator comment\n"
        path.write_text(sentinel)

        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, p: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, p: (True, "ok"))
        assert sm.start("app")[0]  # enabled already True -> no rewrite
        assert path.read_text() == sentinel

        assert sm.stop("app")[0]  # real flip -> rewritten (normalized)
        assert yaml.safe_load(path.read_text())["enabled"] is False


class TestServiceDeclareCli:
    def test_declare_writes_without_applying(self, home):
        result = CliRunner().invoke(
            cli,
            [
                "service",
                "declare",
                "wiki",
                "--image",
                "ghcr.io/a/wiki:1.0",
                "--subdomain",
                "notes",
                "--exposure",
                "tunnel",
                "--port",
                "4567",
                "--critical",
                "true",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["ok"] is True and body["applied"] is False

        decl = yaml.safe_load(services_d.declaration_path(home, "wiki").read_text())
        assert decl["image"] == "ghcr.io/a/wiki:1.0"
        assert decl["traefik"]["subdomain"] == "notes"
        assert decl["traefik"]["exposure"] == "tunnel"
        assert decl["critical"] is True
        # nothing installed/started
        assert not (home / "services" / "wiki").exists()
        assert not (home / "compose" / "wiki.yaml").exists()

    def test_declare_validates_through_trust_boundary(self, home):
        result = CliRunner().invoke(
            cli,
            ["service", "declare", "bad", "--image", "nginx:latest", "--json"],
        )
        assert result.exit_code == 1
        assert "error" in json.loads(result.output)
        assert not services_d.declaration_path(home, "bad").exists()

    def test_declare_updates_existing_declaration(self, home):
        for image in ("ghcr.io/a/app:1.0", "ghcr.io/a/app:2.0"):
            result = CliRunner().invoke(cli, ["service", "declare", "app", "--image", image])
            assert result.exit_code == 0, result.output
        decl = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert decl["image"] == "ghcr.io/a/app:2.0"

    def test_declare_then_reconcile_installs(self, home, monkeypatch):
        assert (
            CliRunner()
            .invoke(cli, ["service", "declare", "app", "--image", "ghcr.io/a/app:1.0"])
            .exit_code
            == 0
        )
        installed = []
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "unknown")
        monkeypatch.setattr(
            ServiceManager,
            "install_declaration",
            lambda self, service, start=True, preserve_data_on_rollback=False: (
                installed.append(service.name) or (True, "installed")
            ),
        )
        result = CliRunner().invoke(cli, ["reconcile", "--json"])
        assert result.exit_code == 0, result.output
        assert installed == ["app"]


class TestStartSelfHeals:
    def test_start_regenerates_compose_and_fixes_volume_dir(self, home, monkeypatch):
        """`start` must re-materialize (regenerate compose -> recreate/chmod the
        volume dir) so a service whose bind-mount dir was left in a bad state
        self-heals — no reconcile action could fix it otherwise."""
        import stat as _stat

        sm = _manager(home)
        # install a volume-declaring service (not started)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", volumes=["data:/data:rw"], start=False)[0]

        # Simulate the 0.3.5-era damage: the volume dir was created root-owned/0755.
        vol_dir = home / "data" / "app" / "data"
        assert vol_dir.is_dir()
        vol_dir.chmod(0o755)
        assert _stat.S_IMODE(vol_dir.stat().st_mode) == 0o755

        # start() should regenerate compose -> _ensure_volume_dir chmods it 0777.
        started = {}
        monkeypatch.setattr(
            ServiceManager,
            "_start_service",
            lambda self, n, p: (started.setdefault("yes", True), (True, "started"))[1],
        )
        ok, _ = sm.start("app")
        assert ok and started.get("yes")
        assert _stat.S_IMODE(vol_dir.stat().st_mode) == 0o777


class TestTolerantLoad:
    """The dashboard (read-only, image-baked syrviscore) tolerates a NEWER top-level
    schema field so a valid declaration isn't flagged 'invalid' — CLI stays strict."""

    def test_strict_flags_unknown_top_level_key(self, home):
        _declare(home, "svc", future_only_field="x")
        valid, invalid = services_d.load_declarations(home)  # strict (default)
        assert "svc" not in valid
        assert any(r["file"] == "svc.yaml" for r in invalid)

    def test_tolerant_loads_despite_unknown_top_level_key(self, home):
        _declare(home, "svc", future_only_field="x")
        valid, invalid = services_d.load_declarations(home, tolerant=True)
        assert "svc" in valid and not invalid

    def test_tolerant_still_reports_real_errors(self, home):
        # a genuine error (declared name != filename) must still surface as invalid
        _declare(home, "svc")
        p = services_d.get_declarations_dir(home) / "svc.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["name"] = "not-svc"
        p.write_text(yaml.safe_dump(doc))
        valid, invalid = services_d.load_declarations(home, tolerant=True)
        assert "svc" not in valid and "not-svc" not in valid
        assert any(r["file"] == "svc.yaml" for r in invalid)

    def test_tolerant_matches_strict_for_clean_declarations(self, home):
        _declare(home, "svc")
        assert (
            services_d.load_declarations(home, tolerant=True)[0].keys()
            == services_d.load_declarations(home)[0].keys()
        )
