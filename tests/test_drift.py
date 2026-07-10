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
