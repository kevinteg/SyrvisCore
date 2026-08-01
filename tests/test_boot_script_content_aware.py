"""
Content-aware rc.d S99 boot script (writer + validator).

The old presence-only installer/validator never noticed CONTENT drift, which is
why the home-tech design/28 graceful-shutdown flush (the ``stop)`` case that runs
``syrvis shutdown --reason reboot``) never reached a NAS whose S99 predated it —
and nothing flagged it. These tests pin that:

  * ``render_boot_script`` includes the design/28 stop-case, and
  * ``check_boot_script`` compares the deployed S99 against the current render and
    flags any difference (e.g. a stale hook missing the stop-case) as fixable.

The DSM installer writes to the hardcoded ``/usr/local/etc/rc.d/`` path (root +
DSM only), so its content-aware rewrite is exercised through the shared
``_write_script_if_changed`` helper it wraps, against a tmp file.
"""

from pathlib import Path

from syrviscore import privileged_ops
from syrviscore.validators import SystemValidator


class TestRenderBootScript:
    def test_includes_design28_stop_case(self, tmp_path):
        content = privileged_ops.render_boot_script(tmp_path / "install")
        assert "shutdown --reason reboot" in content
        assert "stop)" in content

    def test_start_case_invokes_startup_script(self, tmp_path):
        install_dir = tmp_path / "install"
        content = privileged_ops.render_boot_script(install_dir)
        assert str(install_dir / "bin" / "syrvis-startup.sh") in content


class TestWriteScriptIfChanged:
    def test_created_when_absent(self, tmp_path):
        target = tmp_path / "rc.d" / "S99syrviscore.sh"
        changed, state = privileged_ops._write_script_if_changed(target, "hello\n", 0o755)
        assert changed is True
        assert state == "created"
        assert target.read_text() == "hello\n"
        assert (target.stat().st_mode & 0o777) == 0o755

    def test_unchanged_when_identical(self, tmp_path):
        target = tmp_path / "S99syrviscore.sh"
        privileged_ops._write_script_if_changed(target, "hello\n", 0o755)
        changed, state = privileged_ops._write_script_if_changed(target, "hello\n", 0o755)
        assert changed is False
        assert state == "unchanged"

    def test_updated_on_content_drift(self, tmp_path):
        target = tmp_path / "S99syrviscore.sh"
        privileged_ops._write_script_if_changed(target, "old\n", 0o755)
        changed, state = privileged_ops._write_script_if_changed(target, "new\n", 0o755)
        assert changed is True
        assert state == "updated"
        assert target.read_text() == "new\n"


def _patch_s99_path(monkeypatch, s99: Path) -> None:
    """Redirect the hardcoded ``/usr/local/etc/rc.d/S99syrviscore.sh`` lookup in
    ``check_boot_script`` to a tmp file, leaving every other ``Path(...)`` call
    untouched."""

    real_path = Path

    def fake_path(*args, **kwargs):
        if args and str(args[0]).endswith("S99syrviscore.sh"):
            return s99
        return real_path(*args, **kwargs)

    monkeypatch.setattr("syrviscore.validators.Path", fake_path)


def _stale_s99_without_stop_case(install_dir: Path) -> str:
    """A pre-design/28 S99 render: start-case only, no graceful-flush stop-case."""
    startup = install_dir / "bin" / "syrvis-startup.sh"
    return (
        "#!/bin/sh\n"
        "# SyrvisCore boot script\n\n"
        'case "$1" in\n'
        "    start)\n"
        f'        if [ -x "{startup}" ]; then\n'
        f'            "{startup}"\n'
        "        fi\n"
        "        ;;\n"
        "    stop)\n"
        "        ip link del syrvis-shim 2>/dev/null || true\n"
        "        ;;\n"
        "esac\n"
        "exit 0\n"
    )


class TestBootScriptValidatorContentCheck:
    def test_matching_render_passes(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "install"
        s99 = tmp_path / "S99syrviscore.sh"
        s99.write_text(privileged_ops.render_boot_script(install_dir))
        _patch_s99_path(monkeypatch, s99)
        result = SystemValidator(install_dir, username="cerebrate").check_boot_script()
        assert result.passed
        assert not result.fixable

    def test_stale_stop_case_is_unhealthy_and_fixable(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "install"
        s99 = tmp_path / "S99syrviscore.sh"
        # Deployed S99 predates design/28 — missing the graceful-flush stop-case.
        s99.write_text(_stale_s99_without_stop_case(install_dir))
        assert "shutdown --reason reboot" not in s99.read_text()

        _patch_s99_path(monkeypatch, s99)
        result = SystemValidator(install_dir, username="cerebrate").check_boot_script()
        assert not result.passed
        assert result.fixable
        assert result.fix_action == "boot_script"

    def test_missing_is_fixable(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "install"
        s99 = tmp_path / "does-not-exist" / "S99syrviscore.sh"
        _patch_s99_path(monkeypatch, s99)
        result = SystemValidator(install_dir, username="cerebrate").check_boot_script()
        assert not result.passed
        assert result.fixable
        assert result.fix_action == "boot_script"
