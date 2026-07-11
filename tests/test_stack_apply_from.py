"""CLI tests for whole-set convergence (`stack apply --from`) and the catalog
surface (`service catalog`, `service run <name>` without --image)."""

import json

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore.cli import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    (h / "config" / "stack.yaml").write_text(
        "version: 1\n"
        "services:\n"
        "  traefik: {enabled: true}\n"
        "  portainer: {enabled: true}\n"
        "  cloudflared: {enabled: true}\n"
        "  dashboard: {enabled: false}\n"
        "  cloudflare_ddns: {enabled: false}\n"
    )
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.setenv("DOMAIN", "example.com")
    monkeypatch.setattr(cli_mod.privilege, "ensure_elevated", lambda reason: None)
    return h


def _desired(tmp_path, doc):
    f = tmp_path / "desired.yaml"
    f.write_text(yaml.safe_dump(doc))
    return str(f)


class TestStackApplyFrom:
    def test_dry_run_json_emits_plan_without_applying(self, home, tmp_path):
        desired = _desired(
            tmp_path, {"services": {"wiki": {"image": "ghcr.io/a/wiki:1.0", "port": 4567}}}
        )
        result = CliRunner().invoke(
            cli, ["stack", "apply", "--from", desired, "--dry-run", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["applied"] is False
        assert body["plan"]["actions"][0]["kind"] == "service_add"
        # dry run installed nothing
        assert not (home / "services" / "wiki").exists()

    def test_invalid_desired_yields_json_error_envelope(self, home, tmp_path):
        desired = _desired(tmp_path, {"nonsense": True})
        result = CliRunner().invoke(cli, ["stack", "apply", "--from", desired, "--json"])
        assert result.exit_code == 1
        assert "error" in json.loads(result.output)

    def test_destructive_actions_require_confirmation(self, home, tmp_path, monkeypatch):
        from syrviscore.service_manager import ServiceManager

        sm = ServiceManager(syrvis_home=home)
        assert sm.add_image("old", "ghcr.io/a/old:1.0", start=False)[0]

        desired = _desired(tmp_path, {"services": {}, "on_undeclared": "purge"})
        # answer "n" to the confirm -> aborted, nothing removed
        result = CliRunner().invoke(cli, ["stack", "apply", "--from", desired], input="n\n")
        assert result.exit_code != 0
        assert (home / "services" / "old").exists()

    def test_apply_converges_and_reports_results(self, home, tmp_path, monkeypatch):
        desired = _desired(
            tmp_path,
            {
                "stack": {"dashboard": {"enabled": True, "subdomain": "dash"}},
                "services": {},
            },
        )
        # stack change triggers a compose regen; stub it (no .env in this home)
        monkeypatch.setattr(cli_mod, "_regenerate_compose", lambda: (True, "regenerated"))
        result = CliRunner().invoke(cli, ["stack", "apply", "--from", desired, "--json", "-y"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["applied"] is True
        assert body["results"][0]["kind"] == "stack_enable"
        assert body["results"][0]["ok"] is True
        assert body["regen"] == "regenerated"
        # the stack.yaml was actually updated
        stack = yaml.safe_load((home / "config" / "stack.yaml").read_text())
        assert stack["services"]["dashboard"]["enabled"] is True

    def test_dry_run_without_from_is_an_error(self, home):
        result = CliRunner().invoke(cli, ["stack", "apply", "--dry-run"])
        assert result.exit_code == 1
        assert "--from" in result.output


class TestServiceCatalogCli:
    def test_catalog_lists_bundled_templates(self, home):
        result = CliRunner().invoke(cli, ["service", "catalog"])
        assert result.exit_code == 0, result.output
        assert "gollum" in result.output
        assert "uptime-kuma" in result.output

    def test_catalog_json(self, home):
        result = CliRunner().invoke(cli, ["service", "catalog", "--json"])
        assert result.exit_code == 0
        names = {t["name"] for t in json.loads(result.output)["templates"]}
        assert {"gollum", "uptime-kuma", "homeassistant"} <= names

    def test_run_without_image_resolves_from_catalog(self, home):
        result = CliRunner().invoke(
            cli, ["service", "run", "gollum", "--subdomain", "notes", "--no-start"]
        )
        assert result.exit_code == 0, result.output
        manifest = yaml.safe_load(
            (home / "services" / "gollum" / "syrvis-service.yaml").read_text()
        )
        assert manifest["image"].startswith("gollum/gollum:")
        assert manifest["traefik"]["subdomain"] == "notes"

    def test_run_unknown_catalog_name_fails_helpfully(self, home):
        result = CliRunner().invoke(cli, ["service", "run", "no-such-thing", "--no-start"])
        assert result.exit_code == 1
        assert "--image" in result.output

    def test_volume_flag_requires_image(self, home):
        result = CliRunner().invoke(
            cli, ["service", "run", "gollum", "--volume", "x:/x", "--no-start"]
        )
        assert result.exit_code == 1
        assert "--volume" in result.output

    def test_run_with_image_and_v2_flags(self, home):
        result = CliRunner().invoke(
            cli,
            [
                "service",
                "run",
                "app",
                "--image",
                "ghcr.io/a/app:1.0",
                "--volume",
                "data:/app/data:rw",
                "--env-file",
                "secrets.env",
                "--no-start",
            ],
        )
        assert result.exit_code == 0, result.output
        manifest = yaml.safe_load((home / "services" / "app" / "syrvis-service.yaml").read_text())
        assert manifest["volumes"] == ["data:/app/data:rw"]
        assert manifest["env_file"] == "secrets.env"
        # the env file was materialized 0600 in the service data dir
        env_file = home / "data" / "app" / "secrets.env"
        assert env_file.exists()
        import stat

        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
