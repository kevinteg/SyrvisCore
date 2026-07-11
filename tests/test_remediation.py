"""
Tests for the shared remediation dispatch and verify --fix orchestration.

The privileged operations themselves are faked (they need root + docker); these
tests pin the dispatch mapping and the --fix control flow.
"""

from pathlib import Path

import pytest

from syrviscore import remediation, verify


class FakeOps:
    """Records calls instead of touching the system."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, args))
        return True, f"{name} ok"

    def ensure_docker_group(self):
        return self._record("docker_group")

    def ensure_user_in_docker_group(self, user):
        return self._record("user_group", user)

    def ensure_docker_socket_permissions(self):
        return self._record("socket_perms")

    def ensure_global_symlink(self, install_dir):
        return self._record("symlink", install_dir)

    def ensure_startup_script(self, install_dir, user):
        return self._record("startup", install_dir, user)

    def ensure_boot_script(self, install_dir):
        return self._record("boot_script", install_dir)

    def ensure_manifest_permissions(self, install_dir):
        return self._record("manifest_perms", install_dir)


@pytest.fixture
def fake_ops(monkeypatch):
    ops = FakeOps()
    monkeypatch.setattr(remediation, "privileged_ops", ops)
    return ops


class TestApplyFixDispatch:
    def test_docker_group(self, fake_ops):
        ok, _ = remediation.apply_fix("docker_group", None)
        assert ok
        assert fake_ops.calls == [("docker_group", ())]

    def test_user_group_extracts_user(self, fake_ops):
        remediation.apply_fix("user_group:kevin", None)
        assert fake_ops.calls == [("user_group", ("kevin",))]

    def test_socket_perms(self, fake_ops):
        remediation.apply_fix("socket_perms", None)
        assert fake_ops.calls == [("socket_perms", ())]

    def test_boot_script_needs_install_dir(self, fake_ops):
        ok, msg = remediation.apply_fix("boot_script", None)
        assert not ok
        assert "install directory" in msg
        assert fake_ops.calls == []

    def test_boot_script_with_install_dir(self, fake_ops):
        d = Path("/volume1/syrviscore")
        ok, _ = remediation.apply_fix("boot_script", d)
        assert ok
        assert fake_ops.calls == [("boot_script", (d,))]

    def test_startup_extracts_user_and_needs_dir(self, fake_ops):
        d = Path("/volume1/syrviscore")
        remediation.apply_fix("startup:kevin", d)
        assert fake_ops.calls == [("startup", (d, "kevin"))]

    def test_manifest_perms(self, fake_ops):
        d = Path("/volume1/syrviscore")
        remediation.apply_fix("manifest_perms", d)
        assert fake_ops.calls == [("manifest_perms", (d,))]

    def test_unknown_action_reports_not_wired(self, fake_ops):
        ok, msg = remediation.apply_fix("teleport", None)
        assert not ok
        assert "No automatic fix wired up" in msg
        assert fake_ops.calls == []


class TestVerifyRemediate:
    def test_remediate_applies_validator_fixes(self, monkeypatch):
        from syrviscore.validators import CheckResult, ValidationReport

        report = ValidationReport(category="Docker Access")
        report.checks.append(
            CheckResult(
                name="Docker group",
                passed=False,
                message="missing",
                fixable=True,
                fix_action="docker_group",
            )
        )
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [report])
        monkeypatch.setattr(verify.remediation, "resolve_install_dir", lambda: None)

        applied = []
        monkeypatch.setattr(
            verify.remediation,
            "apply_fix",
            lambda action, install_dir: applied.append(action) or (True, "fixed"),
        )
        # No drift path
        monkeypatch.setattr(verify, "gather_core_drift", lambda: _StubDrift(in_sync=True))

        actions = verify.remediate(smoke=True)
        assert applied == ["docker_group"]
        assert actions[0]["target"] == "Docker group"
        assert actions[0]["ok"] is True

    def test_remediate_reconciles_drift(self, monkeypatch):
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        monkeypatch.setattr(verify.remediation, "resolve_install_dir", lambda: None)
        monkeypatch.setattr(verify, "gather_core_drift", lambda: _StubDrift(in_sync=False))

        called = {}

        def fake_reconcile():
            called["recon"] = True
            return True, "reconciled"

        monkeypatch.setattr(verify, "_reconcile_core_drift", fake_reconcile)
        actions = verify.remediate(smoke=True)
        assert called.get("recon")
        assert actions[-1]["target"] == "core-drift"
        assert actions[-1]["ok"] is True

    def test_remediate_skips_reconcile_when_in_sync(self, monkeypatch):
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        monkeypatch.setattr(verify.remediation, "resolve_install_dir", lambda: None)
        monkeypatch.setattr(verify, "gather_core_drift", lambda: _StubDrift(in_sync=True))

        called = {}

        def fake_reconcile():
            called["recon"] = True
            return True, "x"

        monkeypatch.setattr(verify, "_reconcile_core_drift", fake_reconcile)
        actions = verify.remediate(smoke=True)
        assert "recon" not in called
        assert actions == []

    def test_remediate_restarts_traefik_on_stale_static_config(self, monkeypatch):
        """STALE_STATIC drift gets a targeted Traefik restart, not a compose up
        (up -d cannot apply a bind-mounted static-config change)."""
        from syrviscore import drift as drift_mod

        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        monkeypatch.setattr(verify.remediation, "resolve_install_dir", lambda: None)
        stale = drift_mod.DriftItem(
            service="traefik",
            kind=drift_mod.DriftKind.STALE_STATIC,
            expected="2026-07-11T02:04:07+00:00",
            actual="2026-07-11T01:49:59Z",
        )
        monkeypatch.setattr(
            verify, "gather_core_drift", lambda: _StubDrift(in_sync=False, items=[stale])
        )

        called = {}
        monkeypatch.setattr(
            verify, "_reconcile_core_drift", lambda: called.setdefault("recon", True) or (True, "x")
        )
        import syrviscore.docker_manager as dm

        monkeypatch.setattr(
            dm, "restart_traefik_if_running", lambda: called.setdefault("restart", True) or True
        )

        actions = verify.remediate(smoke=True)
        # Only the stale-static item: no compose reconcile, just the restart.
        assert "recon" not in called
        assert called.get("restart")
        assert actions[-1]["target"] == "traefik-static-config"
        assert actions[-1]["ok"] is True


class TestVerifyFixCommand:
    def test_fix_elevates_and_remediates_when_unhealthy(self, monkeypatch):
        from click.testing import CliRunner

        from syrviscore.cli import cli

        elevated = {}
        monkeypatch.setattr(
            verify.privilege, "ensure_elevated", lambda reason: elevated.setdefault("yes", True)
        )
        # unhealthy first, healthy after remediation
        reports = iter(
            [
                {"smoke": True, "healthy": False, "checks": [], "drift": None},
                {"smoke": True, "healthy": True, "checks": [], "drift": None},
            ]
        )
        monkeypatch.setattr(verify, "build_report", lambda smoke: next(reports))
        remediated = {}
        monkeypatch.setattr(
            verify,
            "remediate",
            lambda smoke: remediated.setdefault("yes", True)
            or [{"target": "core-drift", "action": "compose_up", "ok": True, "message": "done"}],
        )

        result = CliRunner().invoke(cli, ["verify", "--fix", "--json"])
        assert result.exit_code == 0
        assert elevated.get("yes")
        assert remediated.get("yes")

    def test_fix_skips_remediation_when_already_healthy(self, monkeypatch):
        from click.testing import CliRunner

        from syrviscore.cli import cli

        monkeypatch.setattr(verify.privilege, "ensure_elevated", lambda reason: None)
        monkeypatch.setattr(
            verify,
            "build_report",
            lambda smoke: {"smoke": smoke, "healthy": True, "checks": [], "drift": None},
        )
        called = {}
        monkeypatch.setattr(verify, "remediate", lambda smoke: called.setdefault("yes", True) or [])

        result = CliRunner().invoke(cli, ["verify", "--fix"])
        assert result.exit_code == 0
        assert "yes" not in called  # nothing to fix -> remediate not called


class _StubDrift:
    """Stub of drift.DriftReport carrying real DriftItems so kind checks work."""

    def __init__(self, in_sync, items=None):
        from syrviscore import drift as drift_mod

        self.in_sync = in_sync
        if items is None:
            items = (
                []
                if in_sync
                else [
                    drift_mod.DriftItem(
                        service="traefik",
                        kind=drift_mod.DriftKind.MISSING,
                        expected="traefik:v3",
                    )
                ]
            )
        self.items = items

    @property
    def failures(self):
        return [i for i in self.items if i.is_failure]
