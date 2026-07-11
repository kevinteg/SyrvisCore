"""
Tests for the read-only drift-detection engine and the verify report builder.

All pure/injectable: no docker, no privileged operations.
"""

import textwrap

import pytest

from syrviscore import drift, verify
from syrviscore.drift import DriftKind


class TestImageNormalization:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("traefik:v3.0.0", "library/traefik:v3.0.0"),
            ("traefik:v3.0.0", "docker.io/library/traefik:v3.0.0"),
            ("portainer/portainer-ce:2.19.4", "portainer/portainer-ce:2.19.4"),
        ],
    )
    def test_equivalent_images_match(self, a, b):
        assert drift.images_match(a, b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("traefik:v3.0.0", "traefik:v3.1.0"),
            ("portainer/portainer-ce:2.19.4", "portainer/portainer-ce:2.20.0"),
            ("traefik:v3.0.0", "nginx:v3.0.0"),
        ],
    )
    def test_different_images_do_not_match(self, a, b):
        assert not drift.images_match(a, b)


class TestExpectedFromCompose:
    def test_parses_services_and_images(self, tmp_path):
        compose = tmp_path / "docker-compose.yaml"
        compose.write_text(
            textwrap.dedent(
                """
                services:
                  traefik:
                    image: traefik:v3.0.0
                  portainer:
                    image: portainer/portainer-ce:2.19.4
                networks:
                  proxy:
                    external: true
                """
            )
        )
        expected = drift.expected_services_from_compose(compose)
        assert expected == {
            "traefik": "traefik:v3.0.0",
            "portainer": "portainer/portainer-ce:2.19.4",
        }

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            drift.expected_services_from_compose(tmp_path / "nope.yaml")

    def test_malformed_compose_raises(self, tmp_path):
        bad = tmp_path / "docker-compose.yaml"
        bad.write_text("just a string")
        with pytest.raises(ValueError):
            drift.expected_services_from_compose(bad)


EXPECTED = {
    "traefik": "traefik:v3.0.0",
    "portainer": "portainer/portainer-ce:2.19.4",
    "cloudflared": "cloudflare/cloudflared:2024.1.0",
}


class TestDetectDrift:
    def test_all_in_sync(self):
        actual = {
            "traefik": {"status": "running", "image": "library/traefik:v3.0.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
            "cloudflared": {"status": "running", "image": "cloudflare/cloudflared:2024.1.0"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        assert report.in_sync
        assert report.items == []

    def test_missing_container(self):
        actual = {
            "traefik": {"status": "running", "image": "traefik:v3.0.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        assert not report.in_sync
        kinds = {(i.service, i.kind) for i in report.items}
        assert ("cloudflared", DriftKind.MISSING) in kinds

    def test_stopped_container(self):
        actual = {
            "traefik": {"status": "exited", "image": "traefik:v3.0.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
            "cloudflared": {"status": "running", "image": "cloudflare/cloudflared:2024.1.0"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        stopped = [i for i in report.items if i.kind is DriftKind.STOPPED]
        assert len(stopped) == 1
        assert stopped[0].service == "traefik"
        assert stopped[0].actual == "exited"

    def test_image_mismatch(self):
        actual = {
            "traefik": {"status": "running", "image": "traefik:v2.11.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
            "cloudflared": {"status": "running", "image": "cloudflare/cloudflared:2024.1.0"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        mismatch = [i for i in report.items if i.kind is DriftKind.IMAGE_MISMATCH]
        assert len(mismatch) == 1
        assert mismatch[0].service == "traefik"
        assert mismatch[0].expected == "traefik:v3.0.0"
        assert mismatch[0].actual == "traefik:v2.11.0"

    def test_unexpected_container_is_warning_not_failure(self):
        actual = {
            "traefik": {"status": "running", "image": "traefik:v3.0.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
            "cloudflared": {"status": "running", "image": "cloudflare/cloudflared:2024.1.0"},
            "rogue": {"status": "running", "image": "evil:1.0"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        unexpected = [i for i in report.items if i.kind is DriftKind.UNEXPECTED]
        assert len(unexpected) == 1
        assert unexpected[0].service == "rogue"
        assert not unexpected[0].is_failure
        # An unexpected container alone does not make the scope out of sync
        assert report.in_sync

    def test_stopped_and_wrong_image_both_reported(self):
        actual = {
            "traefik": {"status": "exited", "image": "traefik:v2.0.0"},
            "portainer": {"status": "running", "image": "portainer/portainer-ce:2.19.4"},
            "cloudflared": {"status": "running", "image": "cloudflare/cloudflared:2024.1.0"},
        }
        report = drift.detect_drift("core", EXPECTED, actual)
        traefik_kinds = {i.kind for i in report.items if i.service == "traefik"}
        assert DriftKind.STOPPED in traefik_kinds
        assert DriftKind.IMAGE_MISMATCH in traefik_kinds

    def test_to_dict_shape(self):
        actual = {"traefik": {"status": "running", "image": "traefik:v3.0.0"}}
        report = drift.detect_drift("core", {"traefik": "traefik:v3.0.0"}, actual)
        d = report.to_dict()
        assert d["scope"] == "core"
        assert d["in_sync"] is True
        assert isinstance(d["items"], list)


class TestVerifyReportBuilder:
    def _patch_compose(self, monkeypatch, tmp_path):
        compose = tmp_path / "docker-compose.yaml"
        compose.write_text(
            textwrap.dedent(
                """
                services:
                  traefik:
                    image: traefik:v3.0.0
                """
            )
        )
        monkeypatch.setattr(verify.paths, "get_docker_compose_path", lambda: compose)

    def test_healthy_when_validators_pass_and_in_sync(self, monkeypatch, tmp_path):
        self._patch_compose(monkeypatch, tmp_path)
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        actual = {"traefik": {"status": "running", "image": "traefik:v3.0.0"}}

        result = verify.build_report(smoke=True, actual=actual)
        assert result["healthy"] is True
        assert result["drift"]["in_sync"] is True

    def test_unhealthy_on_drift(self, monkeypatch, tmp_path):
        self._patch_compose(monkeypatch, tmp_path)
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        actual = {"traefik": {"status": "exited", "image": "traefik:v3.0.0"}}

        result = verify.build_report(smoke=True, actual=actual)
        assert result["healthy"] is False

    def test_unhealthy_on_failing_validator(self, monkeypatch, tmp_path):
        self._patch_compose(monkeypatch, tmp_path)

        from syrviscore.validators import CheckResult, ValidationReport

        report = ValidationReport(category="Installation")
        report.checks.append(CheckResult(name="Manifest", passed=False, message="missing"))
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [report])
        actual = {"traefik": {"status": "running", "image": "traefik:v3.0.0"}}

        result = verify.build_report(smoke=True, actual=actual)
        assert result["healthy"] is False
        assert any(c["name"] == "Manifest" and not c["passed"] for c in result["checks"])

    def test_missing_compose_does_not_crash(self, monkeypatch, tmp_path):
        monkeypatch.setattr(verify.paths, "get_docker_compose_path", lambda: tmp_path / "nope.yaml")
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])

        result = verify.build_report(smoke=True, actual={})
        # compose absent -> drift reports an error but validators were fine
        assert "error" in result["drift"]


class TestVerifyCommand:
    def test_verify_json_healthy(self, monkeypatch):
        import json

        from click.testing import CliRunner

        from syrviscore.cli import cli

        monkeypatch.setattr(
            verify,
            "build_report",
            lambda smoke: {"smoke": smoke, "healthy": True, "checks": [], "drift": None},
        )
        result = CliRunner().invoke(cli, ["verify", "--smoke", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["healthy"] is True

    def test_verify_exits_nonzero_when_unhealthy(self, monkeypatch):
        from click.testing import CliRunner

        from syrviscore.cli import cli

        monkeypatch.setattr(
            verify,
            "build_report",
            lambda smoke: {"smoke": smoke, "healthy": False, "checks": [], "drift": None},
        )
        result = CliRunner().invoke(cli, ["verify"])
        assert result.exit_code == 1
        assert "UNHEALTHY" in result.output


class TestStaleStaticConfig:
    """The STALE_STATIC drift kind: static config newer than the running process."""

    def test_parse_docker_timestamp_nanoseconds(self):
        # Docker emits RFC3339 with nanosecond fractions Python 3.8 can't parse raw.
        parsed = drift.parse_docker_timestamp("2026-07-11T15:18:14.123456789Z")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert (parsed.year, parsed.minute, parsed.second) == (2026, 18, 14)

    def test_parse_docker_timestamp_garbage(self):
        assert drift.parse_docker_timestamp("") is None
        assert drift.parse_docker_timestamp("not-a-time") is None

    def test_static_config_is_stale(self):
        started = "2026-07-11T08:49:59Z"
        started_epoch = drift.parse_docker_timestamp(started).timestamp()
        # File written AFTER the process started -> stale.
        assert drift.static_config_is_stale(started_epoch + 60, started) is True
        # File written BEFORE the process started -> fine.
        assert drift.static_config_is_stale(started_epoch - 60, started) is False
        # Unparseable StartedAt -> unknown (None), never a false positive.
        assert drift.static_config_is_stale(started_epoch, "garbage") is None

    def test_stale_static_is_a_failing_kind_with_description(self):
        item = drift.DriftItem(
            service="traefik",
            kind=drift.DriftKind.STALE_STATIC,
            expected="2026-07-11T02:04:07+00:00",
            actual="2026-07-11T01:49:59Z",
        )
        assert item.is_failure
        assert "restart traefik" in item.describe()
        assert item.to_dict()["kind"] == "stale_static_config"


class TestL2Drift:
    def _report(self, monkeypatch, items):
        stub = drift.DriftReport(scope="layer2", items=items)
        monkeypatch.setattr(verify, "gather_l2_drift", lambda: stub)
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        monkeypatch.setattr(
            verify,
            "gather_core_drift",
            lambda actual=None: drift.DriftReport(scope="core", items=[]),
        )
        return verify.build_report(smoke=True)

    def test_noncritical_l2_failure_degrades_but_stays_healthy(self, monkeypatch):
        """The design rule: a non-critical service must never block — verify
        reports DEGRADED (exit 0), not unhealthy."""
        result = self._report(
            monkeypatch,
            [drift.DriftItem(service="wiki", kind=DriftKind.MISSING, expected="a:1")],
        )
        assert result["l2_drift"]["in_sync"] is False
        assert result["healthy"] is True
        assert result["degraded"] is True

    def test_critical_l2_failure_is_unhealthy(self, monkeypatch):
        result = self._report(
            monkeypatch,
            [
                drift.DriftItem(
                    service="vital", kind=DriftKind.MISSING, expected="a:1", critical=True
                )
            ],
        )
        assert result["healthy"] is False
        assert result["l2_drift"]["items"][0]["critical"] is True

    def test_in_sync_l2_is_neither(self, monkeypatch):
        result = self._report(monkeypatch, [])
        assert result["healthy"] is True
        assert result["degraded"] is False

    def test_gather_l2_drift_uses_declarations(self, monkeypatch, tmp_path):
        """Declared-enabled-but-missing = drift (critical honored); declared-
        disabled is skipped; unmanaged installs still watched (non-critical)."""
        import yaml as yamllib

        from syrviscore import services_d
        from syrviscore.service_manager import ServiceManager

        home = tmp_path / "syrviscore"
        (home / "config").mkdir(parents=True)
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        monkeypatch.setenv("DOMAIN", "example.com")
        d = services_d.get_declarations_dir(home)
        d.mkdir(parents=True)
        (d / "vital.yaml").write_text(
            yamllib.safe_dump(
                {"name": "vital", "version": "1", "image": "ghcr.io/a/vital:1.0", "critical": True}
            )
        )
        (d / "napping.yaml").write_text(
            yamllib.safe_dump(
                {"name": "napping", "version": "1", "image": "ghcr.io/a/nap:1.0", "enabled": False}
            )
        )
        # unmanaged install (no declaration)
        sm = ServiceManager(syrvis_home=home)
        assert sm.add_image("legacy", "ghcr.io/a/legacy:1.0", start=False)[0]
        services_d.remove_declaration(home, "legacy")

        # docker unreachable for containers -> everything expected is MISSING
        import docker as docker_sdk

        class _Boom:
            def __init__(self):
                self.containers = self

            def get(self, name):
                raise RuntimeError("no docker in tests")

        monkeypatch.setattr(docker_sdk, "from_env", lambda: _Boom())

        report = verify.gather_l2_drift()
        by_name = {i.service: i for i in report.items}
        assert by_name["vital"].critical is True  # declared critical, missing
        assert by_name["legacy"].critical is False  # unmanaged, watched
        assert "napping" not in by_name  # declared-off: skipped

    def test_remediate_starts_failing_l2_services(self, monkeypatch):
        stub = drift.DriftReport(
            scope="layer2",
            items=[drift.DriftItem(service="wiki", kind=DriftKind.STOPPED, actual="exited")],
        )
        monkeypatch.setattr(verify, "gather_l2_drift", lambda: stub)
        monkeypatch.setattr(verify, "run_validation_reports", lambda smoke: [])
        monkeypatch.setattr(verify.remediation, "resolve_install_dir", lambda: None)
        monkeypatch.setattr(
            verify,
            "gather_core_drift",
            lambda actual=None: drift.DriftReport(scope="core", items=[]),
        )

        started = []

        class FakeSM:
            def start(self, name):
                started.append(name)
                return True, "started"

        import syrviscore.service_manager as sm_mod

        monkeypatch.setattr(sm_mod, "ServiceManager", FakeSM)
        actions = verify.remediate(smoke=True)
        assert started == ["wiki"]
        assert actions[-1]["target"] == "l2:wiki"
        assert actions[-1]["ok"] is True
