"""Tests for ServiceManager.write_config() and the `syrvis config set` CLI verb.

The jobs analog of test_write_secret.py: write_config renders a DECLARED job's
config/<name>.conf (root:root 0600) from stdin, exactly mirroring write_secret's
security contract but gated on config/jobs.d instead of config/services.d.
"""

import os
import stat
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_syrvis_home(tmp_path: Path) -> Path:
    """Return a minimal syrvis_home with the required directory layout."""
    home = tmp_path / "syrviscore"
    (home / "config" / "jobs.d").mkdir(parents=True)
    (home / "jobs").mkdir(parents=True)
    return home


def _declare_job(home: Path, name: str, schedule: str = "0 3 * * *", enabled: bool = True) -> Path:
    """Write a minimal jobs.d declaration for *name* ({schedule, enabled} only)."""
    body = "schedule: '{}'\nenabled: {}\n".format(schedule, "true" if enabled else "false")
    path = home / "config" / "jobs.d" / f"{name}.yaml"
    path.write_text(body)
    return path


def _mk_manager(home: Path):
    from syrviscore.service_manager import ServiceManager

    return ServiceManager(syrvis_home=home)


# ---------------------------------------------------------------------------
# write_config: happy path
# ---------------------------------------------------------------------------


class TestWriteConfigHappy:
    def test_writes_file_with_correct_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        manager = _mk_manager(home)
        ok, msg = manager.write_config("login-alert", "NTFY_URL=https://ntfy.example/topic\n")

        assert ok, f"expected success, got: {msg}"
        dest = home / "config" / "login-alert.conf"
        assert dest.exists()
        assert dest.read_text() == "NTFY_URL=https://ntfy.example/topic\n"

    def test_file_mode_is_0600(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        manager = _mk_manager(home)
        manager.write_config("login-alert", "NTFY_URL=x\n")

        dest = home / "config" / "login-alert.conf"
        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_second_write_overwrites_cleanly(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")
        manager = _mk_manager(home)

        manager.write_config("login-alert", "NTFY_URL=old\n")
        ok, _ = manager.write_config("login-alert", "NTFY_URL=new\n")

        dest = home / "config" / "login-alert.conf"
        assert ok
        assert dest.read_text() == "NTFY_URL=new\n"

    def test_message_contains_destination_path(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")
        manager = _mk_manager(home)

        ok, msg = manager.write_config("login-alert", "K=v\n")

        assert ok
        assert "login-alert.conf" in msg

    def test_no_temp_file_left_behind(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")
        manager = _mk_manager(home)

        manager.write_config("login-alert", "K=v\n")

        leftover = list((home / "config").glob("*.tmp"))
        assert leftover == [], f"temp file(s) left behind: {leftover}"

    def test_disabled_job_is_still_writable(self, tmp_path):
        """A declared-but-disabled job is still a declared job — its conf can be
        rendered (it is simply unscheduled)."""
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "immich-db-backup", enabled=False)
        manager = _mk_manager(home)

        ok, msg = manager.write_config("immich-db-backup", "NTFY_URL=x\n")

        assert ok, msg
        assert (home / "config" / "immich-db-backup.conf").exists()


# ---------------------------------------------------------------------------
# write_config: error cases (mirror write_secret's error suite)
# ---------------------------------------------------------------------------


class TestWriteConfigErrors:
    def test_rejects_undeclared_job(self, tmp_path):
        """The declared-JOB gate: a name not in jobs.d is refused."""
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        ok, msg = manager.write_config("ghost", "K=v\n")

        assert not ok
        assert "not declared" in msg
        assert "jobs.d" in msg
        # And nothing was written.
        assert not (home / "config" / "ghost.conf").exists()

    def test_rejects_empty_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")
        manager = _mk_manager(home)

        ok, msg = manager.write_config("login-alert", "")

        assert not ok
        assert "empty" in msg

    def test_rejects_oversized_content(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")
        manager = _mk_manager(home)

        big = "K=" + "x" * 70000 + "\n"
        ok, msg = manager.write_config("login-alert", big)

        assert not ok
        assert "large" in msg or "size" in msg or "65536" in msg

    def test_rejects_invalid_name(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        ok, msg = manager.write_config("UPPER_CASE", "K=v\n")

        assert not ok

    def test_rejects_traversal_name(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        manager = _mk_manager(home)

        # NAME_RE rejects '..' / '/' directly; defense in depth against traversal.
        ok, _msg = manager.write_config("../escape", "K=v\n")

        assert not ok
        # No file escaped the config dir.
        assert not (tmp_path / "escape.conf").exists()

    def test_rejects_missing_config_dir(self, tmp_path):
        """Fails (does NOT mkdir the home) if config/ is absent — a broken install."""
        home = tmp_path / "syrviscore"  # deliberately NO config/ created
        home.mkdir()
        manager = _mk_manager(home)

        ok, msg = manager.write_config("login-alert", "K=v\n")

        assert not ok
        # It fails the declared-job gate first (jobs.d cannot load), which is the
        # more restrictive outcome — either way nothing is written.
        assert not (home / "config").exists()


# ---------------------------------------------------------------------------
# CLI: `syrvis config set` via CliRunner (mirror TestCLISecretSet)
# ---------------------------------------------------------------------------


class TestCLIConfigSet:
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
            ["config", "set", name],
            input=stdin,
            catch_exceptions=False,
            env=env,
        )
        return result

    def test_happy_path_echoes_path(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        result = self._run("login-alert", "NTFY_URL=https://ntfy.example/t\n", home)

        assert result.exit_code == 0, result.output
        assert "login-alert.conf" in result.output

    def test_content_not_in_output(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        result = self._run("login-alert", "NTFY_URL=topsecret\n", home)

        # The conf body must never appear in any CLI output.
        assert "topsecret" not in result.output
        assert "topsecret" not in (result.stderr or "")

    def test_empty_stdin_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        result = self._run("login-alert", "", home)

        assert result.exit_code != 0

    def test_undeclared_job_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)

        result = self._run("ghost", "K=v\n", home)

        assert result.exit_code != 0

    def test_oversized_stdin_exits_nonzero(self, tmp_path):
        home = _make_syrvis_home(tmp_path)
        _declare_job(home, "login-alert")

        result = self._run("login-alert", "K=" + "x" * 70000 + "\n", home)

        assert result.exit_code != 0
