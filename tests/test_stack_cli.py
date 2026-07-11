"""CliRunner tests for the `syrvis stack` command group."""

import json

import pytest
from click.testing import CliRunner

from syrviscore.cli import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    (h / ".syrviscore-manifest.json").write_text(
        '{"schema_version":3,"active_version":"0.2.0","versions":{}}'
    )
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    return h


@pytest.fixture
def runner():
    return CliRunner()


def _services(output):
    return {s["service"]: s for s in json.loads(output)["services"]}


def test_stack_list_json_defaults(home, runner):
    r = runner.invoke(cli, ["stack", "list", "--json"])
    assert r.exit_code == 0, r.output
    svcs = _services(r.output)
    assert svcs["traefik"]["primordial"] and svcs["traefik"]["enabled"]
    assert svcs["dashboard"]["enabled"] is False  # opt-in default
    assert svcs["cloudflared"]["enabled"] is True  # migration default preserves it


def test_stack_list_text(home, runner):
    r = runner.invoke(cli, ["stack", "list"])
    assert r.exit_code == 0
    assert "SyrvisCore stack" in r.output


def test_enable_persists_and_lists(home, runner):
    assert (
        runner.invoke(cli, ["stack", "enable", "dashboard", "--subdomain", "panel"]).exit_code == 0
    )
    assert (home / "config" / "stack.yaml").exists()
    svcs = _services(runner.invoke(cli, ["stack", "list", "--json"]).output)
    assert svcs["dashboard"]["enabled"] is True
    assert svcs["dashboard"]["settings"]["subdomain"] == "panel"


def test_disable_persists(home, runner):
    runner.invoke(cli, ["stack", "enable", "cloudflared"])
    runner.invoke(cli, ["stack", "disable", "cloudflared"])
    svcs = _services(runner.invoke(cli, ["stack", "list", "--json"]).output)
    assert svcs["cloudflared"]["enabled"] is False


def test_disable_primordial_rejected(home, runner):
    r = runner.invoke(cli, ["stack", "disable", "portainer"])
    assert r.exit_code != 0
    assert "primordial" in r.output.lower()


def test_enable_unknown_rejected(home, runner):
    r = runner.invoke(cli, ["stack", "enable", "nope"])
    assert r.exit_code != 0
    assert "unknown" in r.output.lower()


def test_ddns_hint_when_enabled_without_token(home, runner):
    runner.invoke(cli, ["stack", "enable", "cloudflare_ddns"])
    svcs = _services(runner.invoke(cli, ["stack", "list", "--json"]).output)
    assert "CLOUDFLARE_API_TOKEN" in svcs["cloudflare_ddns"]["note"]
