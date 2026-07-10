"""
CLI contract tests for syrvisctl (click.testing.CliRunner).

These pin the command surface the MCP server will later be built on: exit
codes, --json output shapes, and error messages. The venv backend and the
GitHub downloader are faked — no network, no real pip.
"""

import json

import pytest
from click.testing import CliRunner

from syrviscore_manager import downloader, manifest, paths, version_manager
from syrviscore_manager.cli import cli
from syrviscore_manager.errors import NetworkError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SYRVIS_HOME", raising=False)
    monkeypatch.delenv("SYNOPKG_PKGDEST", raising=False)
    monkeypatch.delenv("DSM_SIM_ACTIVE", raising=False)
    monkeypatch.delenv("DSM_SIM_ROOT", raising=False)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "syrviscore"


@pytest.fixture
def fake_venv_backend(monkeypatch):
    def _create_venv(venv_path):
        (venv_path / "bin").mkdir(parents=True, exist_ok=True)
        (venv_path / "bin" / "pip").write_text("#!/bin/sh\n")

    def _pip_install_wheel(venv_path, wheel_path):
        (venv_path / "bin" / "syrvis").write_text(
            "#!/bin/sh\n# venv: {}\necho fake\n".format(venv_path)
        )

    monkeypatch.setattr(version_manager, "_create_venv", _create_venv)
    monkeypatch.setattr(version_manager, "_pip_install_wheel", _pip_install_wheel)


def make_wheel(tmp_path, version="0.1.0"):
    wheel = tmp_path / "syrviscore-{}-py3-none-any.whl".format(version)
    wheel.write_bytes(b"fake")
    return wheel


def cli_install_wheel(runner, home, wheel):
    return runner.invoke(cli, ["install", "--wheel", str(wheel), "--path", str(home), "-y"])


class TestInstallWheel:
    def test_install_from_local_wheel(self, runner, home, tmp_path, fake_venv_backend):
        wheel = make_wheel(tmp_path, "0.2.0")
        result = cli_install_wheel(runner, home, wheel)

        assert result.exit_code == 0, result.output
        assert "Installation complete!" in result.output
        assert paths.active_version(home) == "0.2.0"

    def test_reinstall_without_force_fails(self, runner, home, tmp_path, fake_venv_backend):
        wheel = make_wheel(tmp_path, "0.2.0")
        assert cli_install_wheel(runner, home, wheel).exit_code == 0

        result = cli_install_wheel(runner, home, wheel)
        assert result.exit_code == 1
        assert "already installed" in result.output

        result = runner.invoke(
            cli, ["install", "--wheel", str(wheel), "--path", str(home), "-y", "--force"]
        )
        assert result.exit_code == 0, result.output


class TestListCommand:
    def test_list_json_empty(self, runner):
        result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"versions": [], "active": None}

    def test_list_json_and_human(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        for v in ("0.1.0", "0.2.0"):
            assert cli_install_wheel(runner, home, make_wheel(tmp_path, v)).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["list", "--json"])
        data = json.loads(result.output)
        assert data["versions"] == ["0.2.0", "0.1.0"]
        assert data["active"] == "0.2.0"

        result = runner.invoke(cli, ["list"])
        assert "0.2.0 (active)" in result.output


class TestUninstallCommand:
    def test_uninstall_active_refused(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["uninstall", "0.1.0", "-y"])
        assert result.exit_code == 1
        assert "active version" in result.output

    def test_uninstall_invalid_version_rejected(
        self, runner, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["uninstall", "../..", "-y"])
        assert result.exit_code == 1
        assert "Invalid version" in result.output
        assert home.exists()

    def test_uninstall_inactive_version(
        self, runner, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        for v in ("0.1.0", "0.2.0"):
            assert cli_install_wheel(runner, home, make_wheel(tmp_path, v)).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["uninstall", "0.1.0", "-y"])
        assert result.exit_code == 0, result.output
        assert not (home / "versions" / "0.1.0").exists()


class TestActivateCommand:
    def test_activate_switches(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        for v in ("0.1.0", "0.2.0"):
            assert cli_install_wheel(runner, home, make_wheel(tmp_path, v)).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["activate", "0.1.0"])
        assert result.exit_code == 0, result.output
        assert paths.active_version(home) == "0.1.0"

        result = runner.invoke(cli, ["activate", "0.1.0"])
        assert "already active" in result.output

    def test_activate_missing_lists_installed(
        self, runner, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["activate", "0.9.9"])
        assert result.exit_code == 1
        assert "not installed" in result.output


class TestInfoCommand:
    def test_info_json_not_installed(self, runner):
        result = runner.invoke(cli, ["info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["installed"] is False

    def test_info_json_installed(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["info", "--json"])
        data = json.loads(result.output)
        assert data["installed"] is True
        assert data["active"] == "0.1.0"
        assert data["home"] == str(home)
        assert "0.1.0" in data["versions"]


class TestCheckCommand:
    def test_check_json(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        monkeypatch.setattr(
            downloader,
            "get_latest_release",
            lambda: {"tag_name": "v0.2.0", "body": "notes"},
        )

        result = runner.invoke(cli, ["check", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {
            "current": "0.1.0",
            "latest": "0.2.0",
            "update_available": True,
            "release_notes": "notes",
        }

    def test_check_network_error_is_clean(self, runner, monkeypatch):
        def boom():
            raise NetworkError("GitHub API returned HTTP 403")

        monkeypatch.setattr(downloader, "get_latest_release", boom)
        result = runner.invoke(cli, ["check"])
        assert result.exit_code == 1
        assert "Error: GitHub API returned HTTP 403" in result.output
        # No traceback leaks to the user
        assert "Traceback" not in result.output


class TestCleanupCommand:
    def test_cleanup_dry_run_and_apply(
        self, runner, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        for v in ("0.1.0", "0.2.0", "0.3.0"):
            assert cli_install_wheel(runner, home, make_wheel(tmp_path, v)).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["cleanup", "--keep", "2", "--dry-run"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output
        assert (home / "versions" / "0.1.0").exists()

        result = runner.invoke(cli, ["cleanup", "--keep", "2", "-y"])
        assert result.exit_code == 0, result.output
        assert not (home / "versions" / "0.1.0").exists()
        assert (home / "versions" / "0.3.0").exists()


class TestBackupCommands:
    def test_backup_create_and_list_json(
        self, runner, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = runner.invoke(cli, ["backup", "create"])
        assert result.exit_code == 0, result.output
        assert "Backup complete" in result.output

        result = runner.invoke(cli, ["backup", "list", "--json"])
        data = json.loads(result.output)
        assert len(data["backups"]) == 1
        assert data["backups"][0]["version"] == "0.1.0"

    def test_restore_from_backup_file(self, runner, home, tmp_path, fake_venv_backend, monkeypatch):
        assert cli_install_wheel(runner, home, make_wheel(tmp_path, "0.1.0")).exit_code == 0
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        result = runner.invoke(cli, ["backup", "create"])
        assert result.exit_code == 0, result.output
        backup_file = home / "backups" / "0.1.0.tar.gz"
        assert backup_file.exists()

        target = tmp_path / "dr-restore"
        result = runner.invoke(cli, ["restore", str(backup_file), "--path", str(target), "-y"])
        assert result.exit_code == 0, result.output
        assert "Restore complete" in result.output
        assert paths.active_version(target) == "0.1.0"
        assert manifest.get_active_version(target) == "0.1.0"
