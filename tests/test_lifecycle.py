"""Tests for lifecycle states, transition hooks, runstate, and graceful shutdown."""

import json
import os

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore import lifecycle, services_d
from syrviscore.cli import cli
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import ServiceDefinition, ServiceValidationError

from conftest import stamp_install_root


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    stamp_install_root(h)
    monkeypatch.setenv("DOMAIN", "example.com")
    monkeypatch.delenv("SYRVIS_IN_HOOK", raising=False)
    monkeypatch.setattr(cli_mod.privilege, "ensure_elevated", lambda reason: None)
    return h


def _manager(home):
    return ServiceManager(syrvis_home=home)


def _write_hook(home, workload, event, body="#!/bin/sh\nexit 0\n", mode=0o755):
    d = home / "hooks.d" / workload
    d.mkdir(parents=True, exist_ok=True)
    path = d / event
    path.write_text(body)
    path.chmod(mode)
    return path


class TestDeriveState:
    @pytest.mark.parametrize(
        "observed,enabled,expected",
        [
            ("running", True, "running"),
            ("running", None, "running"),
            ("restarting", False, "failed"),
            ("dead", None, "failed"),
            ("exited", True, "failed"),
            ("exited", False, "stopped"),
            ("exited", None, "stopped"),
            ("created", True, "stopped"),
            ("paused", True, "stopped"),
            ("stopped", True, "stopped"),
            ("weird", True, "unknown"),
            ("", None, "unknown"),
        ],
    )
    def test_container_states(self, observed, enabled, expected):
        assert lifecycle.derive_state(observed, enabled) == expected

    @pytest.mark.parametrize(
        "power,enabled,expected",
        [
            ("running", True, "running"),
            ("transition", True, "running"),
            ("stopped", True, "failed"),
            ("stopped", False, "stopped"),
            ("not_found", True, "unknown"),
            ("unknown", True, "unknown"),
        ],
    )
    def test_vm_states(self, power, enabled, expected):
        assert lifecycle.derive_vm_state(power, enabled) == expected


class TestHostHookTrust:
    def test_owned_executable_runs(self, home):
        marker = home / "ran"
        _write_hook(home, "app", "pre-stop", "#!/bin/sh\ntouch {}\n".format(marker))
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert run.ran and run.ok
        assert marker.exists()

    def test_absent_hook_is_silent_noop(self, home):
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and run.ok and run.skipped_reason is None

    @pytest.mark.parametrize("mode", [0o775, 0o777])
    def test_group_or_world_writable_skipped(self, home, mode):
        _write_hook(home, "app", "pre-stop", mode=mode)
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and run.ok and "writable" in run.skipped_reason

    def test_group_writable_parent_dir_skipped(self, home):
        path = _write_hook(home, "app", "pre-stop")
        path.parent.chmod(0o775)
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and "writable" in run.skipped_reason

    def test_symlink_skipped(self, home):
        real = home / "evil.sh"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(0o755)
        d = home / "hooks.d" / "app"
        d.mkdir(parents=True)
        os.symlink(str(real), str(d / "pre-stop"))
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and "symlink" in run.skipped_reason

    def test_non_executable_skipped(self, home):
        _write_hook(home, "app", "pre-stop", mode=0o644)
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and "executable" in run.skipped_reason

    def test_failing_hook_reports_not_ok(self, home):
        _write_hook(home, "app", "pre-stop", "#!/bin/sh\necho boom >&2\nexit 3\n")
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert run.ran and not run.ok and run.rc == 3
        assert "boom" in run.output_tail

    def test_env_contract(self, home):
        out = home / "env-dump"
        _write_hook(
            home,
            "app",
            "pre-stop",
            "#!/bin/sh\nenv > {}\n".format(out),
        )
        lifecycle.run_host_hook(
            home, "pre-stop", "service", "app", reason="ups", context={"image": "x:1"}
        )
        dump = out.read_text()
        assert "SYRVIS_HOOK_EVENT=pre-stop" in dump
        assert "SYRVIS_HOOK_WORKLOAD=app" in dump
        assert "SYRVIS_HOOK_REASON=ups" in dump
        assert "SYRVIS_HOOK_IMAGE=x:1" in dump
        assert "SYRVIS_IN_HOOK=1" in dump

    def test_reentrancy_fuse(self, home, monkeypatch):
        marker = home / "ran"
        _write_hook(home, "app", "pre-stop", "#!/bin/sh\ntouch {}\n".format(marker))
        monkeypatch.setenv("SYRVIS_IN_HOOK", "1")
        run = lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        assert not run.ran and run.ok
        assert not marker.exists()

    def test_instance_scope_uses_instance_dir(self, home):
        marker = home / "instance-ran"
        _write_hook(home, "instance", "pre-shutdown", "#!/bin/sh\ntouch {}\n".format(marker))
        run = lifecycle.run_host_hook(home, "pre-shutdown", "instance")
        assert run.ran and run.ok and marker.exists()

    def test_hook_log_written(self, home):
        _write_hook(home, "app", "pre-stop")
        lifecycle.run_host_hook(home, "pre-stop", "service", "app")
        log = (home / "logs" / "hooks.log").read_text()
        assert json.loads(log.splitlines()[0])["event"] == "pre-stop"


class TestHooksSchema:
    def _doc(self, **extra):
        doc = {
            "name": "db",
            "version": "1",
            "image": "ghcr.io/a/db:1.0",
            "tasks": {"checkpoint": {"command": ["pg_ctl", "checkpoint"]}},
        }
        doc.update(extra)
        return doc

    def test_valid_hooks_roundtrip(self):
        svc = ServiceDefinition.from_dict(self._doc(hooks={"pre-stop": "checkpoint"}))
        assert svc.hooks == {"pre-stop": "checkpoint"}
        assert svc.to_dict()["hooks"] == {"pre-stop": "checkpoint"}

    def test_invalid_event_rejected(self):
        with pytest.raises(ServiceValidationError, match="not supported"):
            ServiceDefinition.from_dict(self._doc(hooks={"pre-deploy": "checkpoint"}))

    def test_undeclared_task_rejected(self):
        with pytest.raises(ServiceValidationError, match="undeclared task"):
            ServiceDefinition.from_dict(self._doc(hooks={"pre-stop": "nope"}))

    def test_shutdown_block_roundtrip_and_bounds(self):
        svc = ServiceDefinition.from_dict(self._doc(shutdown={"stop_timeout": 120, "priority": 90}))
        assert svc.shutdown == {"stop_timeout": 120, "priority": 90}
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(self._doc(shutdown={"stop_timeout": 1}))
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(self._doc(shutdown={"unknown": 1}))

    def test_compose_emits_stop_grace_period(self, home):
        sm = _manager(home)
        sm._ensure_directories()
        svc = ServiceDefinition.from_dict(self._doc(shutdown={"stop_timeout": 120}))
        (home / "services" / "db").mkdir(parents=True, exist_ok=True)
        path = sm._generate_compose_file(svc)
        compose = yaml.safe_load(path.read_text())
        assert compose["services"]["db"]["stop_grace_period"] == "120s"


class TestVerbHooks:
    def _install(self, home, name="app", **extra):
        sm = _manager(home)
        doc = {"name": name, "version": "1", "image": "ghcr.io/a/{}:1.0".format(name)}
        doc.update(extra)
        assert sm.install_declaration(ServiceDefinition.from_dict(doc), start=False)[0]
        return sm

    def test_pre_start_failure_aborts_unless_forced(self, home, monkeypatch):
        sm = self._install(home)
        _write_hook(home, "app", "pre-start", "#!/bin/sh\nexit 1\n")
        started = []
        monkeypatch.setattr(
            ServiceManager, "_start_service", lambda self, n, c: started.append(n) or (True, "ok")
        )
        ok, msg = sm.start("app")
        assert not ok and "pre-start hook" in msg
        assert not started
        ok, msg = sm.start("app", force=True)
        assert ok
        assert started == ["app"]

    def test_pre_stop_failure_never_blocks_stop(self, home, monkeypatch):
        sm = self._install(home)
        _write_hook(home, "app", "pre-stop", "#!/bin/sh\nexit 1\n")
        stopped = []
        monkeypatch.setattr(
            ServiceManager,
            "_stop_service",
            lambda self, n, c, **kw: stopped.append(kw) or (True, "ok"),
        )
        ok, _ = sm.stop("app")
        assert ok and stopped

    def test_container_hook_fires_before_host_on_stop(self, home, monkeypatch):
        order = []
        sm = self._install(
            home,
            tasks={"checkpoint": {"command": ["sync"]}},
            hooks={"pre-stop": "checkpoint"},
        )
        marker = home / "host-ran"
        _write_hook(home, "app", "pre-stop", "#!/bin/sh\ntouch {}\n".format(marker))
        monkeypatch.setattr(
            ServiceManager,
            "_docker_exec_task",
            lambda self, svc, argv, t: order.append(("container", list(argv))) or (True, "ok"),
        )
        real_host = lifecycle.run_host_hook

        def spy_host(home_, event, scope, workload="", **kw):
            run = real_host(home_, event, scope, workload, **kw)
            if run.ran:
                order.append(("host", event))
            return run

        monkeypatch.setattr(lifecycle, "run_host_hook", spy_host)
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c, **kw: (True, "ok"))
        ok, _ = sm.stop("app")
        assert ok
        assert order[0] == ("container", ["sync"])
        assert ("host", "pre-stop") in order

    def test_stop_operational_mode_keeps_intent_and_containers(self, home, monkeypatch):
        sm = self._install(home)
        calls = []
        monkeypatch.setattr(
            ServiceManager,
            "_compose",
            lambda self, n, p, *args, **kw: calls.append(args) or (True, "ok"),
        )
        ok, _ = sm.stop("app", remove=False, set_intent=False, stop_timeout=45)
        assert ok
        assert calls == [("stop", "-t", "45")]
        decl = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert decl.get("enabled", True) is True  # intent untouched

    def test_default_stop_still_flips_intent(self, home, monkeypatch):
        sm = self._install(home)
        monkeypatch.setattr(
            ServiceManager, "_compose", lambda self, n, p, *args, **kw: (True, "ok")
        )
        ok, _ = sm.stop("app")
        assert ok
        decl = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert decl["enabled"] is False

    def test_reconcile_replace_fires_single_deploy_pair_no_stop_hooks(self, home, monkeypatch):
        sm = self._install(home)
        events = []
        original = ServiceManager._fire_hooks

        def spy(self, target, event, **kw):
            events.append(event)
            return original(self, target, event, **kw)

        monkeypatch.setattr(ServiceManager, "_fire_hooks", spy)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, c, **kw: (True, "ok"))
        decl_path = services_d.declaration_path(home, "app")
        decl = yaml.safe_load(decl_path.read_text())
        decl["image"] = "ghcr.io/a/app:2.0"
        decl_path.write_text(yaml.safe_dump(decl))

        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        services_d.apply_reconcile_plan(sm, declarations, plan)
        assert events.count("pre-deploy") == 1
        assert events.count("post-deploy") == 1
        assert "pre-stop" not in events


class TestRunstate:
    def test_roundtrip_and_clear(self, home):
        record = lifecycle.write_halted(
            home, "ups", [{"name": "app", "scope": "service", "state": "running"}]
        )
        assert record["resume_on_boot"] is True
        state = lifecycle.read_runstate(home)
        assert state["reason"] == "ups"
        assert lifecycle.is_halted(home)
        mode = (home / "data" / "state" / "runstate.json").stat().st_mode & 0o777
        assert mode == 0o644
        assert lifecycle.clear_runstate(home) is True
        assert not lifecycle.is_halted(home)
        assert lifecycle.clear_runstate(home) is False

    def test_maintenance_defaults_to_hold(self, home):
        record = lifecycle.write_halted(home, "maintenance", [])
        assert record["resume_on_boot"] is False

    def test_reboot_defaults_to_auto_resume(self, home):
        # A DSM-initiated reboot (design/28 rc.d-stop hook) must auto-resume on
        # the next boot, same as a ups power event.
        record = lifecycle.write_halted(home, "reboot", [])
        assert record["resume_on_boot"] is True
        assert lifecycle.read_runstate(home)["reason"] == "reboot"

    def test_corrupt_runstate_reads_as_active(self, home):
        path = home / "data" / "state" / "runstate.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        assert lifecycle.read_runstate(home) is None
        assert not lifecycle.is_halted(home)

    def test_guard(self, home):
        lifecycle.guard_not_halted("start", home=home)  # active: passes
        lifecycle.write_halted(home, "maintenance", [])
        with pytest.raises(lifecycle.InstanceHaltedError):
            lifecycle.guard_not_halted("start", home=home)
        lifecycle.guard_not_halted("resume", allow_halted=True, home=home)

    def test_reconcile_apply_refused_while_halted(self, home):
        sm = _manager(home)
        lifecycle.write_halted(home, "maintenance", [])
        with pytest.raises(lifecycle.InstanceHaltedError):
            services_d.apply_reconcile_plan(sm, {}, {"actions": []})
        # planning stays pure/allowed
        assert services_d.build_reconcile_plan(sm, {}, []) is not None
        # resume path passes allow_halted
        assert services_d.apply_reconcile_plan(sm, {}, {"actions": []}, allow_halted=True) == []


class _FakeVms:
    def __init__(self, rows=None, power_sequence=None):
        self.rows = rows or []
        self.calls = []
        self._power = dict(power_sequence or {})

    def list(self):
        return self.rows

    def stop(self, name, hard=False):
        self.calls.append(("stop", name, hard))
        return "ok"

    def start(self, name):
        self.calls.append(("start", name))
        return "ok"

    def status(self, name):
        import collections

        seq = self._power.get(name)
        power = seq.pop(0) if seq else "stopped"
        return collections.namedtuple("S", "power")(power)

    def declarations(self):
        return []


class _FakeDm:
    def __init__(self, running=("traefik", "portainer", "cloudflared")):
        import collections

        C = collections.namedtuple("C", "name status")
        self._containers = [C(n, "running") for n in running]
        self.calls = []

    def get_core_containers(self):
        return self._containers

    def stop_core_container(self, name, timeout=30):
        self.calls.append(("stop", name, timeout))

    def start_core_services(self, allow_halted=False):
        self.calls.append(("start", allow_halted))
        return []


class TestShutdownOrchestration:
    def _sm_with(self, home, monkeypatch, names):
        sm = _manager(home)
        for name, extra in names.items():
            doc = {"name": name, "version": "1", "image": "ghcr.io/a/{}:1.0".format(name)}
            doc.update(extra)
            assert sm.install_declaration(ServiceDefinition.from_dict(doc), start=False)[0]
        monkeypatch.setattr(
            ServiceManager,
            "list",
            lambda self: [{"name": n, "status": "running"} for n in names],
        )
        calls = []
        monkeypatch.setattr(
            ServiceManager,
            "_compose",
            lambda self, n, p, *args, **kw: calls.append((n,) + args) or (True, "ok"),
        )
        return sm, calls

    def test_full_shutdown_order_and_runstate(self, home, monkeypatch):
        sm, compose_calls = self._sm_with(
            home,
            monkeypatch,
            {
                "postgres": {"shutdown": {"stop_timeout": 120, "priority": 90}},
                "web": {},
            },
        )
        vms = _FakeVms(
            rows=[{"name": "haos", "power": "running"}],
            power_sequence={"haos": ["stopped"]},
        )
        dm = _FakeDm()
        report = lifecycle.shutdown_instance(
            home, reason="ups", service_manager=sm, docker_manager=dm, vm_manager=vms
        )
        # VM ACPI issued before any L2 stop; web (band 50) before postgres (90)
        assert vms.calls[0] == ("stop", "haos", False)
        stops = [c for c in compose_calls if c[1] == "stop"]
        assert [c[0] for c in stops] == ["web", "postgres"]
        # postgres got its declared grace
        assert stops[1][3] == "120"
        # core stopped in order with traefik last
        assert [c[1] for c in dm.calls] == ["cloudflared", "portainer", "traefik"]
        # halted state persisted with the snapshot + ups auto-resume
        state = lifecycle.read_runstate(home)
        assert state["reason"] == "ups" and state["resume_on_boot"] is True
        assert {w["name"] for w in state["workloads"]} == {
            "postgres",
            "web",
            "haos",
            "traefik",
            "portainer",
            "cloudflared",
        }
        assert report["exit"] == 0 and report["ok"]

    def test_vm_straggler_gets_forced(self, home, monkeypatch):
        sm, _ = self._sm_with(home, monkeypatch, {})
        vms = _FakeVms(
            rows=[{"name": "haos", "power": "running"}],
            power_sequence={"haos": ["running", "running", "stopped"]},
        )
        report = lifecycle.shutdown_instance(
            home,
            reason="ups",
            budget_s=6,
            vm_deadline_s=1,
            service_manager=sm,
            docker_manager=_FakeDm(running=()),
            vm_manager=vms,
        )
        assert ("stop", "haos", True) in vms.calls  # hard fallback fired
        assert "vm:haos" in report["forced"]

    def test_shutdown_failure_isolated_and_exit_2(self, home, monkeypatch):
        sm, _ = self._sm_with(home, monkeypatch, {"web": {}})
        monkeypatch.setattr(
            ServiceManager, "_compose", lambda self, n, p, *a, **k: (False, "stuck")
        )
        report = lifecycle.shutdown_instance(
            home,
            reason="ups",
            service_manager=sm,
            docker_manager=_FakeDm(running=()),
            vm_manager=_FakeVms(),
        )
        assert report["exit"] == 2
        assert "service:web" in report["failed"]
        assert lifecycle.is_halted(home)  # halted even on partial failure

    def test_resume_order_core_vms_l2_and_clears(self, home, monkeypatch):
        sm, compose_calls = self._sm_with(home, monkeypatch, {"web": {}})
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "exited")
        monkeypatch.setattr(ServiceManager, "_start_service", lambda self, n, c: (True, "ok"))
        vms = _FakeVms()
        dm = _FakeDm()
        lifecycle.write_halted(home, "ups", [{"name": "haos", "scope": "vm", "state": "running"}])
        report = lifecycle.resume_instance(
            home, service_manager=sm, docker_manager=dm, vm_manager=vms
        )
        assert dm.calls and dm.calls[0][0] == "start"
        assert ("start", "haos") in vms.calls
        assert report["ok"], report
        assert not lifecycle.is_halted(home)

    def test_resume_noop_when_active(self, home):
        report = lifecycle.resume_instance(home)
        assert report["ok"] and not report["changed"]


class TestCliWiring:
    def test_shutdown_cli_json(self, home, monkeypatch):
        monkeypatch.setattr(
            cli_mod.lifecycle if hasattr(cli_mod, "lifecycle") else lifecycle,
            "shutdown_instance",
            lambda h, **kw: {
                "action": "shutdown",
                "reason": kw.get("reason"),
                "resume_on_boot": True,
                "phases": [],
                "ok": True,
                "exit": 0,
            },
        )
        result = CliRunner().invoke(cli, ["shutdown", "--reason", "ups", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["reason"] == "ups"

    def test_resume_cli_noop(self, home):
        result = CliRunner().invoke(cli, ["resume"])
        assert result.exit_code == 0, result.output
        assert "active" in result.output

    def test_reconcile_boot_maintenance_halt_noop(self, home):
        lifecycle.write_halted(home, "maintenance", [])
        result = CliRunner().invoke(cli, ["reconcile", "--boot"])
        assert result.exit_code == 0, result.output
        assert "halted" in result.output
        assert lifecycle.is_halted(home)  # untouched

    def test_reconcile_boot_ups_halt_resumes(self, home, monkeypatch):
        lifecycle.write_halted(home, "ups", [])
        called = {}
        monkeypatch.setattr(
            lifecycle,
            "resume_instance",
            lambda h, **kw: called.update(kw) or {"ok": True, "changed": True, "phases": []},
        )
        result = CliRunner().invoke(cli, ["reconcile", "--boot"])
        assert result.exit_code == 0, result.output
        assert called.get("boot") is True

    def test_reconcile_refused_while_halted(self, home, monkeypatch):
        lifecycle.write_halted(home, "maintenance", [])
        result = CliRunner().invoke(cli, ["reconcile", "--json"])
        assert result.exit_code == 1
        assert "halted" in json.loads(result.output)["error"]

    def test_status_json_carries_runstate(self, home, monkeypatch):
        from syrviscore.docker_manager import DockerManager

        monkeypatch.setattr(DockerManager, "__init__", lambda self: None)
        monkeypatch.setattr(DockerManager, "get_container_status", lambda self: {})
        lifecycle.write_halted(home, "ups", [])
        result = CliRunner().invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["runstate"]["state"] == "halted"

    def test_seam_registry_has_lifecycle_commands(self):
        from syrviscore.seam import registry
        from syrviscore.seam.gen import render_shim

        for cmd_id in ("shutdown", "resume", "restart_graceful"):
            cmd = registry.COMMANDS_BY_ID[cmd_id]
            assert cmd.sudo and not cmd.destructive
        shim = render_shim()
        assert "is_reason()" in shim and "is_revision()" in shim
