"""
Tests for the deployment bundle (design/21): the syrvis-bundle schema, the
atomic ``ServiceManager.deploy_bundle`` apply, and the ``syrvis deploy`` CLI.

A bundle is attacker-controlled input that root turns into filesystem writes, so
the schema tests pin the trust boundary (unknown keys, traversal dests, env_file
collisions, secrets-without-env_file, unpinned images) and the apply tests pin
the invariants: config 0644 / secret 0600, start LAST, atomic rollback, targeted
(never touches another service), and no secret value in any message.
"""

import json
import os
import stat

import pytest
import yaml
from click.testing import CliRunner

from syrviscore.bundle import (
    BUNDLE_API_VERSION,
    BundleValidationError,
    DeployBundle,
)


def base_manifest(**overrides):
    m = {"name": "snmp-exporter", "version": "v0.30.1", "image": "prom/snmp-exporter:v0.30.1"}
    m.update(overrides)
    return m


def base_bundle(**overrides):
    doc = {"service": base_manifest()}
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Schema — DeployBundle.from_dict (the trust boundary)
# ---------------------------------------------------------------------------


class TestBundleSchema:
    def test_valid_full_bundle(self):
        b = DeployBundle.from_dict(
            {
                "apiVersion": BUNDLE_API_VERSION,
                "service": base_manifest(
                    env_file="secrets.env",
                    volumes=["config:/etc/snmp_exporter:ro"],
                    command=["--config.file=/etc/snmp_exporter/snmp.yml"],
                ),
                "configs": [{"dest": "config/snmp.yml", "content": "auths: {}\n"}],
                "secrets": {"SNMP_V3_USER": "snmp-monitor", "SNMP_V3_AUTH_PASS": "pw"},
            }
        )
        assert b.name == "snmp-exporter"
        assert [c.dest for c in b.configs] == ["config/snmp.yml"]
        assert b.secrets["SNMP_V3_USER"] == "snmp-monitor"

    def test_minimal_bundle_manifest_only(self):
        b = DeployBundle.from_dict(base_bundle())
        assert b.configs == [] and b.secrets == {}

    def test_default_apiversion_accepted(self):
        # apiVersion may be omitted (defaults to the current one)
        assert DeployBundle.from_dict(base_bundle()).name == "snmp-exporter"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(evil=1),  # unknown top-level key
            lambda d: d.update(apiVersion="syrvis-bundle/v2"),  # unknown version
            lambda d: d.pop("service"),  # missing manifest
            lambda d: d.update(configs="notalist"),
            lambda d: d.update(secrets=["notamap"]),
        ],
    )
    def test_structural_rejections(self, mutate):
        d = base_bundle()
        mutate(d)
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(d)

    @pytest.mark.parametrize("dest", ["../escape.yml", "/etc/passwd", "a/../../b"])
    def test_config_dest_traversal_rejected(self, dest):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": dest, "content": "x"}]))

    def test_config_dest_cannot_be_env_file(self):
        with pytest.raises(BundleValidationError, match="env_file"):
            DeployBundle.from_dict(
                base_bundle(
                    service=base_manifest(env_file="secrets.env"),
                    configs=[{"dest": "secrets.env", "content": "x"}],
                )
            )

    def test_duplicate_config_dest_rejected(self):
        with pytest.raises(BundleValidationError, match="duplicate"):
            DeployBundle.from_dict(
                base_bundle(
                    configs=[
                        {"dest": "config/a", "content": "1"},
                        {"dest": "config/a", "content": "2"},
                    ]
                )
            )

    def test_config_content_must_be_string(self):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": "config/a", "content": 42}]))

    def test_config_unknown_key_rejected(self):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(
                base_bundle(configs=[{"dest": "config/a", "content": "x", "mode": "0777"}])
            )

    @pytest.mark.parametrize("key", ["1BAD", "has space", "with-dash", ""])
    def test_bad_secret_env_key_rejected(self, key):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(
                base_bundle(service=base_manifest(env_file="secrets.env"), secrets={key: "v"})
            )

    def test_secrets_require_env_file(self):
        with pytest.raises(BundleValidationError, match="env_file"):
            DeployBundle.from_dict(base_bundle(secrets={"A": "b"}))  # no env_file declared

    def test_unpinned_image_rejected_uniformly(self):
        # the inner ServiceDefinition error surfaces as ONE BundleValidationError
        with pytest.raises(BundleValidationError, match="latest"):
            DeployBundle.from_dict(base_bundle(service=base_manifest(image="prom/x:latest")))

    def test_oversize_config_rejected(self):
        big = "x" * (65536 + 1)
        with pytest.raises(BundleValidationError, match="too large"):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": "config/a", "content": big}]))


# ---------------------------------------------------------------------------
# Apply — ServiceManager.deploy_bundle (docker start stubbed)
# ---------------------------------------------------------------------------


def _manager(tmp_path, start_ok=True):
    from syrviscore.service_manager import ServiceManager

    os.environ.setdefault("DOMAIN", "example.com")
    mgr = ServiceManager(syrvis_home=tmp_path)
    mgr._ensure_directories()
    mgr._reload_traefik = lambda: None
    mgr._start_service = lambda name, cp: (start_ok, "started" if start_ok else "boom")
    return mgr


def _snmp_bundle():
    return DeployBundle.from_dict(
        {
            "service": base_manifest(
                env_file="secrets.env",
                volumes=["config:/etc/snmp_exporter:ro"],
                command=["--config.file=/etc/snmp_exporter/snmp.yml", "--config.expand-environment-variables"],
                networks=["proxy"],
            ),
            "configs": [{"dest": "config/snmp.yml", "content": "auths:\n  synology_v3:\n    username: ${SNMP_V3_USER}\n"}],
            "secrets": {"SNMP_V3_USER": "snmp-monitor", "SNMP_V3_AUTH_PASS": "a", "SNMP_V3_PRIV_PASS": "p"},
        }
    )


class TestDeployBundleApply:
    def test_fresh_install_writes_everything(self, tmp_path):
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok, msg
        assert "installed" in msg

        # declaration
        assert (tmp_path / "config" / "services.d" / "snmp-exporter.yaml").exists()
        # config: 0644, content preserved (placeholders intact — not resolved here)
        cfg = tmp_path / "data" / "snmp-exporter" / "config" / "snmp.yml"
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
        assert "${SNMP_V3_USER}" in cfg.read_text()
        # env_file: 0600, all three secrets
        env = tmp_path / "data" / "snmp-exporter" / "secrets.env"
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert env.read_text().count("=") == 3
        # compose carries command + env_file
        compose = yaml.safe_load((tmp_path / "compose" / "snmp-exporter.yaml").read_text())
        svc = compose["services"]["snmp-exporter"]
        assert svc["command"][-1] == "--config.expand-environment-variables"
        assert svc["env_file"]

    def test_update_is_idempotent_and_preserves_data(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        # drop a marker in the data dir; an UPDATE must not wipe it
        marker = tmp_path / "data" / "snmp-exporter" / "keepme"
        marker.write_text("x")
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok and "updated" in msg
        assert marker.exists()

    def test_failed_start_rolls_back_fresh_install(self, tmp_path):
        mgr = _manager(tmp_path, start_ok=False)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert not ok and "failed" in msg
        # fresh install rollback removes the service + its just-created data dir
        assert not (tmp_path / "services" / "snmp-exporter").exists()
        assert not (tmp_path / "data" / "snmp-exporter").exists()
        # ...but the declared intent remains for a retry (written outside rollback)
        assert (tmp_path / "config" / "services.d" / "snmp-exporter.yaml").exists()

    def test_no_secret_value_in_return_message(self, tmp_path):
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok
        assert "snmp-monitor" not in msg and "secret(s)" in msg

    def test_place_config_refuses_to_downgrade_a_0600_file(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        # simulate a pre-existing 0600 secret at a config path
        secret_path = tmp_path / "data" / "snmp-exporter" / "config" / "hush"
        secret_path.write_text("top")
        os.chmod(secret_path, 0o600)
        ok, msg = mgr._place_config("snmp-exporter", "config/hush", "world-readable")
        assert not ok and "secret" in msg.lower()
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600  # untouched

    def test_place_config_confined_to_data_dir(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        ok, msg = mgr._place_config("snmp-exporter", "../../escape", "x")
        assert not ok


# ---------------------------------------------------------------------------
# CLI — syrvis deploy (elevation stubbed; no docker)
# ---------------------------------------------------------------------------


class TestDeployCli:
    def _run(self, monkeypatch, tmp_path, argv, stdin, deploy_impl=None):
        import syrviscore.privilege as privilege
        from syrviscore import service_manager
        from syrviscore.cli import cli

        monkeypatch.setattr(privilege, "ensure_elevated", lambda *a, **k: None)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        if deploy_impl is not None:
            monkeypatch.setattr(service_manager.ServiceManager, "deploy_bundle", deploy_impl)
        return CliRunner().invoke(cli, argv, input=stdin)

    def test_registered(self):
        from syrviscore.cli import cli

        assert "deploy" in cli.commands

    def test_happy_path_calls_manager(self, monkeypatch, tmp_path):
        seen = {}

        def fake_deploy(self, bundle):
            seen["name"] = bundle.name
            return True, "deployed snmp-exporter (installed; 1 config(s), 3 secret(s))"

        bundle = json.dumps(
            {
                "service": base_manifest(env_file="secrets.env"),
                "configs": [{"dest": "config/snmp.yml", "content": "x"}],
                "secrets": {"SNMP_V3_USER": "snmp-monitor"},
            }
        )
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], bundle, fake_deploy)
        assert r.exit_code == 0, r.output
        assert seen["name"] == "snmp-exporter"
        assert "snmp-monitor" not in r.output  # secret never echoed

    def test_name_mismatch_rejected(self, monkeypatch, tmp_path):
        bundle = json.dumps({"service": base_manifest(name="other")})
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], bundle)
        assert r.exit_code != 0
        assert "does not match" in r.output

    def test_invalid_json_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], "{not json")
        assert r.exit_code != 0
        assert "not valid JSON" in r.output

    def test_empty_stdin_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], "")
        assert r.exit_code != 0
