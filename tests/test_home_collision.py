"""Volume-root collision detection: `check_home_collision` + `syrvisctl doctor`.

A ``syrviscore_1`` sibling is the single cheapest, most specific signal the
2026-08-16 failure produces, and the platform was blind to it by construction:
its 22-method self-audit contains not one check that looks at a volume root, and
the two content-aware checks that WOULD have spoken hang off a validator built
from the install directory — i.e. they cannot run when the install directory is
what moved. A human found the cause with ``ls``.

``syrvisctl doctor`` is the other half: the manager runs from the SPK's own
rootfs venv, so it still answers when SYRVIS_HOME does not resolve at all.
"""

import json

import pytest
import yaml
from click.testing import CliRunner

from syrviscore import validators
from syrviscore_manager import doctor as manager_doctor

from conftest import stamp_install_root


@pytest.fixture
def volumes(tmp_path, monkeypatch):
    """A fake DSM volume layout under DSM_SIM_ROOT."""
    monkeypatch.setenv("DSM_SIM_ACTIVE", "1")
    monkeypatch.setenv("DSM_SIM_ROOT", str(tmp_path))
    for n in (4, 5, 6):
        (tmp_path / "volume{}".format(n)).mkdir()
    return tmp_path


def _platform_root(volumes, n, apps=False):
    root = volumes / "volume{}".format(n) / "syrviscore"
    root.mkdir(parents=True, exist_ok=True)
    stamp_install_root(root)
    if apps:
        (root / "apps").mkdir(exist_ok=True)
    return root


def _renamed_root(volumes, n, suffix="_1"):
    root = volumes / "volume{}".format(n) / ("syrviscore" + suffix)
    root.mkdir(parents=True, exist_ok=True)
    stamp_install_root(root)
    return root


def _declare_share(home, share_id, share_name, volume):
    d = home / "shares.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / "{}.yaml".format(share_id)).write_text(
        yaml.safe_dump({"id": share_id, "share_name": share_name, "volume": volume})
    )


class TestCheckHomeCollision:
    def test_clean_layout_passes(self, volumes, monkeypatch):
        _platform_root(volumes, 4)
        monkeypatch.setenv("SYRVIS_HOME", str(volumes / "volume4" / "syrviscore"))
        result = validators.InstallationValidator().check_home_collision()
        assert result.passed
        assert "no 'syrviscore_N' sibling" in result.message

    def test_renamed_sibling_fails_and_carries_the_exact_mv(self, volumes, monkeypatch):
        renamed = _renamed_root(volumes, 4)
        monkeypatch.setenv("SYRVIS_HOME", str(renamed))
        result = validators.InstallationValidator().check_home_collision()

        assert result.passed is False
        assert str(renamed) in result.message
        assert "sudo mv {} {}".format(renamed, renamed.parent / "syrviscore") in result.details
        # NEVER auto-fixable: reclaiming is the boot guard's job, not verify --fix's
        assert result.fixable is False

    def test_reports_every_affected_volume(self, volumes, monkeypatch):
        _renamed_root(volumes, 4)
        _renamed_root(volumes, 6)
        monkeypatch.setenv("SYRVIS_HOME", str(volumes / "volume4" / "syrviscore_1"))
        result = validators.InstallationValidator().check_home_collision()
        assert "2 collision-renamed" in result.message

    def test_armed_share_collision_is_reported_before_the_cold_boot(self, volumes, monkeypatch):
        """The landmine, six days before it fired."""
        home = _platform_root(volumes, 4)
        _platform_root(volumes, 6, apps=True)
        # design/53 M0: a DSM share literally named `syrviscore`, on /volume5
        _declare_share(home, "syrviscore-apps", "syrviscore", "/volume5")
        monkeypatch.setenv("SYRVIS_HOME", str(home))

        result = validators.InstallationValidator().check_home_collision()

        assert result.passed is False
        assert "armed for a cold-boot rename" in result.message
        assert "Rename the SHARE" in result.details
        assert result.fixable is False

    def test_an_unrelated_share_name_is_fine(self, volumes, monkeypatch):
        home = _platform_root(volumes, 4)
        _declare_share(home, "gaming", "Gaming", "/volume5")
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        assert validators.InstallationValidator().check_home_collision().passed

    def test_a_renamed_root_outranks_the_armed_warning(self, volumes, monkeypatch):
        home = _platform_root(volumes, 4)
        _declare_share(home, "syrviscore-apps", "syrviscore", "/volume5")
        _renamed_root(volumes, 6)
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        result = validators.InstallationValidator().check_home_collision()
        assert "collision-renamed" in result.message  # the fired one, not the armed one

    def test_runs_even_when_syrvis_home_cannot_be_resolved(self, volumes, monkeypatch):
        """The state a collision rename PRODUCES — the check must survive it."""
        _renamed_root(volumes, 4)
        monkeypatch.delenv("SYRVIS_HOME", raising=False)
        report = validators.InstallationValidator().validate()
        by_name = {c.name: c for c in report.checks}
        assert by_name["SYRVIS_HOME"].passed is False
        assert by_name["Home collision"].passed is False

    def test_wired_into_the_installation_report(self, volumes, monkeypatch):
        home = _platform_root(volumes, 4)
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        report = validators.validate_installation()
        assert "Home collision" in {c.name for c in report.checks}


class TestSyrvisctlDoctor:
    """The manager-side diagnosis, which needs NO resolvable home."""

    def test_scan_classifies_platform_renamed_and_other(self, volumes):
        _platform_root(volumes, 4)
        _renamed_root(volumes, 6)
        (volumes / "volume6" / "syrviscore.scaffold-20260816").mkdir()

        rows = {r["path"]: r["kind"] for r in manager_doctor.scan_volume_roots()}

        assert rows[str(volumes / "volume4" / "syrviscore")] == "platform"
        assert rows[str(volumes / "volume6" / "syrviscore_1")] == "renamed"
        assert rows[str(volumes / "volume6" / "syrviscore.scaffold-20260816")] == "other"

    def test_boot_hook_missing_is_a_finding(self, volumes, monkeypatch):
        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", volumes / "absent-S99.sh")
        report = manager_doctor.run()
        assert report["boot_hook"]["present"] is False
        assert any("BOOT HOOK" in f for f in report["findings"])

    def test_stale_boot_hook_detected_by_contract_marker(self, volumes, monkeypatch):
        s99 = volumes / "S99.sh"
        s99.write_text("#!/bin/sh\n# an S99 that predates the reclaim guard\n")
        s99.chmod(0o755)
        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", s99)

        hook = manager_doctor.check_boot_hook()

        assert hook["present"] is True
        assert hook["contract"] == 0
        assert hook["stale"] is True

    def test_current_boot_hook_satisfies_the_contract(self, volumes, monkeypatch, tmp_path):
        from syrviscore import privileged_ops

        s99 = volumes / "S99.sh"
        s99.write_text(privileged_ops.render_boot_script(tmp_path / "install"))
        s99.chmod(0o755)
        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", s99)
        monkeypatch.setattr(manager_doctor, "BOOT_ENV_PATH", s99)  # any existing file

        hook = manager_doctor.check_boot_hook()

        assert hook["stale"] is False
        assert hook["contract"] >= manager_doctor.MIN_BOOT_HOOK_CONTRACT

    def test_the_manager_contract_matches_what_the_platform_renders(self):
        """The one fact duplicated across the package boundary — keep it honest."""
        from syrviscore import privileged_ops

        assert manager_doctor.MIN_BOOT_HOOK_CONTRACT == privileged_ops.BOOT_HOOK_CONTRACT

    def test_seam_shells_reverted_to_nologin_are_a_finding(self, volumes, monkeypatch):
        passwd = volumes / "passwd"
        passwd.write_text(
            "root:x:0:0:root:/root:/bin/sh\n"
            "syrvis-operator:x:1030:100::/var/services/homes/o:/sbin/nologin\n"
            "syrvis-reader:x:1031:100::/var/services/homes/r:/bin/sh\n"
        )
        monkeypatch.setattr(manager_doctor, "PASSWD_PATH", passwd)

        rows = {r["user"]: r for r in manager_doctor.check_seam_accounts()}

        assert rows["syrvis-operator"]["ok"] is False
        assert rows["syrvis-reader"]["ok"] is True

    def test_an_unprovisioned_account_is_not_a_failure(self, volumes, monkeypatch):
        passwd = volumes / "passwd"
        passwd.write_text("root:x:0:0:root:/root:/bin/sh\n")
        monkeypatch.setattr(manager_doctor, "PASSWD_PATH", passwd)
        assert all(r["ok"] for r in manager_doctor.check_seam_accounts())

    def test_home_resolution_failure_is_a_finding_not_a_crash(self, volumes, monkeypatch):
        monkeypatch.delenv("SYRVIS_HOME", raising=False)
        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", volumes / "absent")
        report = manager_doctor.run()
        assert report["home"]["resolved"] is None
        assert report["ok"] is False
        assert any(f.startswith("HOME:") for f in report["findings"])

    def test_renamed_root_is_the_headline_finding(self, volumes, monkeypatch):
        _renamed_root(volumes, 4)
        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", volumes / "absent")
        report = manager_doctor.run()
        assert report["findings"][0].startswith("COLLISION:")
        assert "Do NOT run 'syrvisctl install'" in report["findings"][0]


class TestDoctorCli:
    def test_json_output_and_nonzero_exit_on_findings(self, volumes, monkeypatch):
        from syrviscore_manager.cli import cli as manager_cli

        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", volumes / "absent")
        _renamed_root(volumes, 4)

        result = CliRunner().invoke(manager_cli, ["doctor", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["manager_version"]
        assert any(r["kind"] == "renamed" for r in payload["volume_roots"])

    def test_human_output_names_the_renamed_root(self, volumes, monkeypatch):
        from syrviscore_manager.cli import cli as manager_cli

        monkeypatch.setattr(manager_doctor, "BOOT_SCRIPT_PATH", volumes / "absent")
        _renamed_root(volumes, 6)

        result = CliRunner().invoke(manager_cli, ["doctor"])

        assert "RENAMED BY DSM" in result.output
        assert str(volumes / "volume6" / "syrviscore_1") in result.output
