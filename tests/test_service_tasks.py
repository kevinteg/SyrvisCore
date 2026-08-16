"""Declared one-shot tasks (`tasks:` schema + `syrvis service task`).

Tasks are the encapsulated alternative to a break-glass `docker exec`: a
pre-declared, schema-audited argv run inside the service's OWN container. The
schema tests prove the audit (exec form, literal, no '$'); the manager tests
prove the argv comes from the installed manifest only — never the caller.
"""

import subprocess

import pytest
from click.testing import CliRunner

from syrviscore.service_schema import ServiceDefinition, ServiceValidationError, dump_definition

from conftest import stamp_install_root


def manifest(**over):
    d = {
        "name": "db",
        "version": "1.0.0",
        "image": "postgres:16.3",
        "tasks": {
            "init-legal-db": {"command": ["psql", "-U", "postgres", "-c", "CREATE DATABASE x"]}
        },
    }
    d.update(over)
    return d


class TestTasksSchema:
    def test_valid_tasks_parse_and_roundtrip(self):
        svc = ServiceDefinition.from_dict(manifest())
        assert svc.tasks["init-legal-db"][0] == "psql"
        data = svc.to_dict()
        assert data["tasks"]["init-legal-db"]["command"][0] == "psql"
        # round-trips through the strict boundary
        again = ServiceDefinition.from_dict(data)
        assert again.tasks == svc.tasks

    def test_tasks_must_be_mapping(self):
        with pytest.raises(ServiceValidationError, match="tasks must be"):
            ServiceDefinition.from_dict(manifest(tasks=["x"]))

    def test_bad_task_name_rejected(self):
        with pytest.raises(ServiceValidationError, match="Invalid task name"):
            ServiceDefinition.from_dict(manifest(tasks={"Bad Name!": {"command": ["x"]}}))

    def test_shell_form_rejected(self):
        with pytest.raises(ServiceValidationError, match="exec form"):
            ServiceDefinition.from_dict(manifest(tasks={"t": {"command": "psql -c 'x'"}}))

    def test_dollar_rejected(self):
        with pytest.raises(ServiceValidationError, match="'\\$'"):
            ServiceDefinition.from_dict(manifest(tasks={"t": {"command": ["echo", "$HOME"]}}))

    def test_unknown_task_key_rejected(self):
        with pytest.raises(ServiceValidationError, match="unknown keys"):
            ServiceDefinition.from_dict(manifest(tasks={"t": {"command": ["x"], "user": "root"}}))

    def test_missing_command_rejected(self):
        with pytest.raises(ServiceValidationError, match="missing 'command'"):
            ServiceDefinition.from_dict(manifest(tasks={"t": {}}))

    def test_too_many_tasks_rejected(self):
        tasks = {"t{}".format(i): {"command": ["true"]} for i in range(17)}
        with pytest.raises(ServiceValidationError, match="too many tasks"):
            ServiceDefinition.from_dict(manifest(tasks=tasks))


class TestRunTask:
    def _mgr(self, tmp_path, monkeypatch, definition=None):
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        stamp_install_root(tmp_path)
        from syrviscore.service_manager import ServiceManager

        mgr = ServiceManager()
        if definition is not None:
            svc_dir = mgr.services_dir / definition.name
            svc_dir.mkdir(parents=True)
            dump_definition(definition, svc_dir / "syrvis-service.yaml")
        return mgr

    def test_unknown_service(self, tmp_path, monkeypatch):
        mgr = self._mgr(tmp_path, monkeypatch)
        ok, msg = mgr.run_task("db", "init-legal-db")
        assert not ok and "not installed" in msg

    def test_unknown_task_lists_declared(self, tmp_path, monkeypatch):
        svc = ServiceDefinition.from_dict(manifest())
        mgr = self._mgr(tmp_path, monkeypatch, svc)
        ok, msg = mgr.run_task("db", "nope")
        assert not ok and "init-legal-db" in msg

    def test_runs_declared_argv_via_docker_exec(self, tmp_path, monkeypatch):
        svc = ServiceDefinition.from_dict(manifest())
        mgr = self._mgr(tmp_path, monkeypatch, svc)
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="CREATE DATABASE\n", stderr="")

        import syrviscore.service_manager as sm

        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        ok, msg = mgr.run_task("db", "init-legal-db")
        assert ok, msg
        # the argv is exactly manifest content behind `docker exec <container>`
        assert seen["argv"][:3] == ["docker", "exec", "db"]
        assert seen["argv"][3:] == ["psql", "-U", "postgres", "-c", "CREATE DATABASE x"]
        assert "CREATE DATABASE" in msg

    def test_nonzero_exit_reported(self, tmp_path, monkeypatch):
        svc = ServiceDefinition.from_dict(manifest())
        mgr = self._mgr(tmp_path, monkeypatch, svc)
        import syrviscore.service_manager as sm

        monkeypatch.setattr(
            sm.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 2, stdout="", stderr="boom"),
        )
        ok, msg = mgr.run_task("db", "init-legal-db")
        assert not ok and "exit 2" in msg and "boom" in msg


class TestCli:
    def test_registered_and_wired(self, tmp_path, monkeypatch):
        import syrviscore.privilege as privilege
        from syrviscore import service_manager
        from syrviscore.cli import cli

        monkeypatch.setattr(privilege, "ensure_elevated", lambda *a, **k: None)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        stamp_install_root(tmp_path)
        seen = {}

        def fake_run_task(self, name, task):
            seen["args"] = (name, task)
            return True, "task 'init-legal-db' completed"

        monkeypatch.setattr(service_manager.ServiceManager, "run_task", fake_run_task)
        r = CliRunner().invoke(cli, ["service", "task", "--task", "init-legal-db", "--", "db"])
        assert r.exit_code == 0, r.output
        assert seen["args"] == ("db", "init-legal-db")
