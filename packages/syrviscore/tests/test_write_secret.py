"""Tests for ServiceManager.write_secret() and the `syrvis secret set` CLI verb."""

import os
import stat
import textwrap
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_syrvis_home(tmp_path: Path) -> Path:
    """Return a minimal syrvis_home with the required directory layout."""
    home = tmp_path / "syrviscore"
    (home / "config" / "services.d").mkdir(parents=True)
    (home / "data").mkdir(parents=True)
    return home


def _declare_service(home: Path, name: str, env_file: str = "secrets.env") -> Path:
    """Write a minimal services.d declaration for *name* with an env_file."""
    decl = {
        "name": name,
        "version": "0.1.0",
        "image": f"ghcr.io/test/{name}:1.0",
        "description": "test",
        "traefik": {"subdomain": name, "exposure": "internal", "port": 80},
        "env_file": env_file,
        "enabled": True,
        "critical": False,
    }
    path = home / "config" / "services.d" / f"{name}.yaml"
    path.write_text(yaml.dump(decl))
    return path


def _declare_service_no_env_file(home: Path, name: str) -> Path:
    """Write a services.d declaration with NO env_file field."""
    decl = {
        "name": name,
        "version": "0.1.0",
        "image": f"ghcr.io/test/{name}:1.0",
        "description": "test",
        "traefik": {"subdomain": name, "exposure": "internal", "port": 80},
        "enabled": True,
        "critical": False,
    }
    path = home / "config" / "services.d" / f"{name}.yaml"
    path.write_text(yaml.dump(decl))
    return path


def _mk_manager(home: Path):
    from syrviscore.service_manager import ServiceManager

    return ServiceManager(syrvis_home=home)


# ---------------------------------------------------------------------------
# write_secret: happy path
# ---------------------------------------------------------------------------


class TestWriteSecretHappy:
    def test_writes_file_with_correct_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        manager = _mk_manager(home)
        ok, msg = manager.write_secret("myapp", "PASSWORD=s3cr3t\n")

        assert ok, f"expected success, got: {msg}"
        dest = home / "data" / "myapp" / "secrets.env"
        assert dest.exists()
        assert dest.read_text() == "PASSWORD=s3cr3t\n"

    def test_file_mode_is_0600(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        manager = _mk_manager(home)
        manager.write_secret("myapp", "KEY=val\n")

        dest = home / "data" / "myapp" / "secrets.env"
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_second_write_overwrites_cleanly(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)
        manager = _mk_manager(home)

        manager.write_secret("myapp", "OLD=old\n")
        ok, _ = manager.write_secret("myapp", "NEW=new\n")

        dest = home / "data" / "myapp" / "secrets.env"
        assert ok
        assert dest.read_text() == "NEW=new\n"

    def test_message_contains_destination_path(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("myapp", "K=v\n")

        assert ok
        assert "secrets.env" in msg

    def test_no_temp_file_left_behind(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)
        manager = _mk_manager(home)

        manager.write_secret("myapp", "K=v\n")

        leftover = list((home / "data" / "myapp").glob("*.tmp"))
        assert leftover == [], f"temp file(s) left behind: {leftover}"


# ---------------------------------------------------------------------------
# write_secret: error cases
# ---------------------------------------------------------------------------


class TestWriteSecretErrors:
    def test_rejects_undeclared_service(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("ghost", "K=v\n")

        assert not ok
        assert "not declared" in msg

    def test_rejects_declared_service_without_env_file(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service_no_env_file(home, "noenv")
        (home / "data" / "noenv").mkdir(parents=True)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("noenv", "K=v\n")

        assert not ok
        assert "env_file" in msg

    def test_rejects_missing_data_dir(self, tmp_path):
        """Fails if data/<name>/ was never created by reconcile/install."""
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "staged")
        # deliberately do NOT create home/data/staged
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("staged", "K=v\n")

        assert not ok
        assert "data" in msg.lower() or "exist" in msg.lower() or "deploy" in msg.lower()

    def test_rejects_empty_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("myapp", "")

        assert not ok
        assert "empty" in msg

    def test_rejects_oversized_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)
        manager = _mk_manager(home)

        big = "K=" + "x" * 70000 + "\n"
        ok, msg = manager.write_secret("myapp", big)

        assert not ok
        assert "large" in msg or "size" in msg or "65536" in msg

    def test_rejects_invalid_name(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("UPPER_CASE", "K=v\n")

        assert not ok

    def test_rejects_traversal_name(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        # NAME_RE rejects '..' directly; defense in depth
        ok, _msg = manager.write_secret("../escape", "K=v\n")

        assert not ok


# ---------------------------------------------------------------------------
# write_secret: containment via declaration's env_file
# ---------------------------------------------------------------------------


class TestWriteSecretContainment:
    def test_env_file_subdir_is_allowed_within_data_dir(self, tmp_path):
        """An env_file of 'sub/secrets.env' is fine if it stays inside data/<name>/."""
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp", env_file="sub/secrets.env")
        (home / "data" / "myapp" / "sub").mkdir(parents=True)
        manager = _mk_manager(home)

        ok, msg = manager.write_secret("myapp", "K=v\n")

        assert ok, msg
        dest = home / "data" / "myapp" / "sub" / "secrets.env"
        assert dest.exists()
        assert dest.read_text() == "K=v\n"


# ---------------------------------------------------------------------------
# CLI: `syrvis secret set` via CliRunner
# ---------------------------------------------------------------------------


class TestCLISecretSet:
    def _run(self, name: str, stdin: str, home: Path):
        from click.testing import CliRunner
        from syrviscore.cli import cli

        runner = CliRunner(mix_stderr=False)
        env = {
            "SYRVIS_HOME": str(home),
            # Bypass ensure_elevated() / self_elevate() in tests.
            "DSM_SIM_ACTIVE": "1",
        }
        result = runner.invoke(
            cli,
            ["secret", "set", name],
            input=stdin,
            catch_exceptions=False,
            env=env,
        )
        return result

    def test_happy_path_echoes_path(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        result = self._run("myapp", "PASSWORD=secret\n", home)

        assert result.exit_code == 0, result.output
        assert "secrets.env" in result.output

    def test_secret_not_in_output(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        result = self._run("myapp", "PASSWORD=topsecret\n", home)

        # The secret value must never appear in any CLI output.
        assert "topsecret" not in result.output
        assert "topsecret" not in (result.stderr or "")

    def test_empty_stdin_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        result = self._run("myapp", "", home)

        assert result.exit_code != 0

    def test_undeclared_service_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)

        result = self._run("ghost", "K=v\n", home)

        assert result.exit_code != 0

    def test_oversized_stdin_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_service(home, "myapp")
        (home / "data" / "myapp").mkdir(parents=True)

        result = self._run("myapp", "K=" + "x" * 70000 + "\n", home)

        assert result.exit_code != 0
