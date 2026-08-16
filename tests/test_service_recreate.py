"""``ServiceManager.recreate`` — the missing primitive (incident 2026-08-16).

``docker compose restart`` re-runs the SAME container, and Docker bakes a
container's environment in at CREATE time — so a restart cannot pick up a
rewritten ``env_file``. Until this existed, the only way to re-bake env was
``stop`` + ``start``, and ``stop`` is an INTENT verb: it writes
``enabled: false``, so a ``start`` that then fails leaves the service declared
off and reconcile holds it down forever. That trap was hiding inside the repair
procedure for the incident itself.
"""

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore import services_d
from syrviscore.cli import cli
from syrviscore.service_manager import ServiceManager

from conftest import stamp_install_root


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    stamp_install_root(h)
    monkeypatch.setenv("DOMAIN", "example.com")
    monkeypatch.setattr(cli_mod.privilege, "ensure_elevated", lambda reason: None)
    return h


def _installed(home, name="app"):
    sm = ServiceManager(syrvis_home=home)
    sm._reload_traefik = lambda: None
    assert sm.add_image(name, "ghcr.io/a/{}:1.0".format(name), start=False)[0]
    return sm


class TestRecreate:
    def test_runs_force_recreate(self, home):
        sm = _installed(home)
        calls = []
        sm._compose = lambda name, cp, *a, **k: (calls.append(a) or (True, ""))

        ok, msg = sm.recreate("app")

        assert ok, msg
        assert calls == [("up", "-d", "--force-recreate")]

    def test_never_writes_declared_intent(self, home):
        """The whole reason this verb exists instead of stop+start."""
        sm = _installed(home)
        sm._compose = lambda *a, **k: (True, "")
        services_d.set_declared_enabled(home, "app", False)
        before = services_d.declaration_path(home, "app").read_text()

        sm.recreate("app")

        after = services_d.declaration_path(home, "app").read_text()
        assert after == before
        assert yaml.safe_load(after)["enabled"] is False

    def test_a_failed_recreate_leaves_intent_alone_too(self, home):
        sm = _installed(home)
        sm._compose = lambda *a, **k: (False, "boom")
        before = services_d.declaration_path(home, "app").read_text()

        ok, msg = sm.recreate("app")

        assert ok is False and "boom" in msg
        assert services_d.declaration_path(home, "app").read_text() == before

    def test_regenerates_compose_first(self, home):
        """Self-heals host-side drift exactly as start() does."""
        sm = _installed(home)
        sm._compose = lambda *a, **k: (True, "")
        compose = home / "compose" / "app.yaml"
        compose.write_text("corrupted: true\n")

        sm.recreate("app")

        assert "services" in yaml.safe_load(compose.read_text())

    def test_refuses_an_uninstalled_service(self, home):
        sm = ServiceManager(syrvis_home=home)
        ok, msg = sm.recreate("nope")
        assert ok is False and "not installed" in msg

    def test_refuses_a_bad_name_without_touching_the_filesystem(self, home):
        sm = ServiceManager(syrvis_home=home)
        ok, msg = sm.recreate("../escape")
        assert ok is False


class TestRecreateCli:
    def test_registered_and_wired(self, home, monkeypatch):
        seen = {}

        def fake(self, name):
            seen["name"] = name
            return True, "Service 'app' recreated"

        monkeypatch.setattr(ServiceManager, "recreate", fake)
        result = CliRunner().invoke(cli, ["service", "recreate", "app"])

        assert result.exit_code == 0, result.output
        assert seen["name"] == "app"
        assert "recreated" in result.output

    def test_failure_is_a_nonzero_exit(self, home, monkeypatch):
        monkeypatch.setattr(ServiceManager, "recreate", lambda self, name: (False, "nope"))
        result = CliRunner().invoke(cli, ["service", "recreate", "app"])
        assert result.exit_code != 0


class TestSeamRegistration:
    def test_service_recreate_is_on_the_seam(self):
        from syrviscore.seam.registry import COMMANDS_BY_ID

        cmd = COMMANDS_BY_ID["service_recreate"]
        assert cmd.cli == "syrvis"
        assert cmd.subcommand == ["service", "recreate"]
        assert cmd.sudo is True
        assert cmd.destructive is False  # idempotent container replacement
        assert cmd.positional.kind == "name"

    def test_generated_shim_and_sudoers_carry_it(self):
        from syrviscore.seam import gen

        assert "service recreate -- *" in gen.render_sudoers()
        assert '"${5}" = "recreate"' in gen.render_shim()

    def test_syrvisctl_doctor_is_a_read_only_seam_command(self):
        from syrviscore.seam.registry import COMMANDS_BY_ID

        cmd = COMMANDS_BY_ID["doctor"]
        assert cmd.cli == "syrvisctl"
        assert cmd.read_only is True
        assert cmd.sudo is False  # the reader must be able to call it
