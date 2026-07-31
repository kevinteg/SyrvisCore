"""
Content-aware boot-hook validator (SystemValidator.check_startup_script).

Existence alone is not enough: a deployed ``syrvis-startup.sh`` that drifted
behind the code (missing the reconcile/self-heal section) still "exists" but
leaves the estate unable to auto-resume at boot. These tests pin that the check
compares the deployed file against what ``ensure_startup_script`` would render
today and flags any difference as UNHEALTHY + fixable.
"""

from pathlib import Path

from syrviscore import privileged_ops
from syrviscore.validators import SystemValidator


def _write_startup(install_dir: Path, user: str) -> Path:
    """Render + write the current boot hook, returning its path."""
    ok, _ = privileged_ops.DsmOperations().ensure_startup_script(install_dir, user)
    assert ok
    return install_dir / "bin" / "syrvis-startup.sh"


class TestStartupScriptContentCheck:
    def test_fresh_render_passes(self, tmp_path):
        install_dir = tmp_path / "install"
        _write_startup(install_dir, "cerebrate")
        result = SystemValidator(install_dir, username="cerebrate").check_startup_script()
        assert result.passed
        assert not result.fixable

    def test_missing_is_fixable(self, tmp_path):
        install_dir = tmp_path / "install"
        (install_dir / "bin").mkdir(parents=True)  # dir exists, script does not
        result = SystemValidator(install_dir, username="cerebrate").check_startup_script()
        assert not result.passed
        assert result.fixable
        assert result.fix_action == "startup:cerebrate"

    def test_stale_content_is_unhealthy_and_fixable(self, tmp_path):
        install_dir = tmp_path / "install"
        script = _write_startup(install_dir, "cerebrate")
        # Simulate the historical drift: a hook missing the reconcile section.
        script.write_text("#!/bin/bash\n# stale hook\nexit 0\n")
        result = SystemValidator(install_dir, username="cerebrate").check_startup_script()
        assert not result.passed
        assert result.fixable
        assert result.fix_action == "startup:cerebrate"

    def test_benign_invoking_user_mismatch_does_not_flag_drift(self, tmp_path):
        # The deployed hook was written by the setup user 'cerebrate'; verify is
        # run by a different account. Structural content is identical, so this
        # must NOT be reported as drift, and the fix must preserve the embedded
        # user (not rewrite it to the invoking user).
        install_dir = tmp_path / "install"
        _write_startup(install_dir, "cerebrate")
        result = SystemValidator(install_dir, username="someoneelse").check_startup_script()
        assert result.passed
        assert not result.fixable

    def test_fix_action_preserves_embedded_user_when_stale(self, tmp_path):
        # Deployed hook carries 'cerebrate'; content is stale; verify runs as a
        # different user. The fix_action should target the embedded user so the
        # regenerated hook keeps the correct docker-group self-heal line.
        install_dir = tmp_path / "install"
        script = _write_startup(install_dir, "cerebrate")
        script.write_text(
            script.read_text().replace("reconcile --boot", "reconcile")  # structural drift
        )
        result = SystemValidator(install_dir, username="someoneelse").check_startup_script()
        assert not result.passed
        assert result.fix_action == "startup:cerebrate"

    def test_no_install_dir_reports_cannot_check(self, tmp_path):
        result = SystemValidator(None, username="cerebrate").check_startup_script()
        assert not result.passed
        assert not result.fixable
