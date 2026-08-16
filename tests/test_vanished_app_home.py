"""Reconcile must refuse to re-scaffold a vanished app home (incident 2026-08-16).

The convergence engine diffed declaration against manifest and never stat'd the
filesystem under ``location:``, so a vanished ``<location>/syrviscore/apps/<name>``
was indistinguishable from a healthy one. ``resume`` therefore MANUFACTURED the
tree plus four empty ``secrets.env`` files and started the containers against
them; three Postgres containers came one entrypoint check away from initdb'ing
empty clusters over the real databases. The one data guard the platform had
(``_location_change_refusal``) early-returns when the location has not changed,
which is exactly this case.

Two independent brakes are pinned here, because either alone leaves a hole:

  1. the PLANNER emits a ``blocked`` action that apply can never turn into an
     add/start (the high-water mark says the home once held content), and
  2. the COMPOSE GENERATOR refuses to touch() an absent ``env_file`` for a
     service that is already installed — the mechanism that baked empty env into
     four containers.

Plus the floor check: zero declarations against a populated instance is a
mis-rooted config tree, not a converged empty world.
"""

import shutil

import pytest
import yaml

from syrviscore import services_d
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import ServiceValidationError

from conftest import stamp_install_root


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    stamp_install_root(h)
    monkeypatch.setenv("DOMAIN", "example.com")
    return h


@pytest.fixture
def volumes(tmp_path, monkeypatch):
    """Fake ``/volumeN`` roots (the design/26 v2 app-home layout, sim-rooted)."""
    from syrviscore import paths as paths_mod

    root = tmp_path / "volumes"
    monkeypatch.setattr(paths_mod, "resolve_volume_root", lambda loc: root / str(loc).lstrip("/"))
    monkeypatch.setattr(paths_mod, "is_mounted_volume", lambda loc: True)
    (root / "volume6").mkdir(parents=True)
    return root


def _declare(home, name, **extra):
    d = services_d.get_declarations_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "version": "1.0",
        "image": "ghcr.io/a/{}:1.0".format(name),
        "traefik": {"enabled": True, "subdomain": name, "port": 80, "exposure": "internal"},
    }
    doc.update(extra)
    (d / "{}.yaml".format(name)).write_text(yaml.safe_dump(doc))


def _manager(home, monkeypatch):
    sm = ServiceManager(syrvis_home=home)
    sm._start_service = lambda n, cp: (True, "started")
    sm._reload_traefik = lambda: None
    monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, name: "running")
    return sm


def _plan(sm, home):
    decls, invalid = services_d.load_declarations(home)
    return decls, services_d.build_reconcile_plan(sm, decls, invalid)


def _converge(sm, home):
    decls, plan = _plan(sm, home)
    return plan, services_d.apply_reconcile_plan(sm, decls, plan)


def _install_located_app(sm, home, name="immich-db", **extra):
    _declare(home, name, location="/volume6", **extra)
    plan, results = _converge(sm, home)
    assert all(r["ok"] for r in results), results
    return sm._app_home(name)


class TestHighWaterMark:
    def test_recorded_once_the_home_is_materialized(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        app_home = _install_located_app(sm, home)
        assert app_home.is_dir()
        state = sm.read_home_state("immich-db")
        assert state["home_materialized"] is True
        assert state["home"] == str(app_home)

    def test_legacy_service_records_nothing(self, home, volumes, monkeypatch):
        # No location -> data lives under SYRVIS_HOME, whose disappearance is
        # caught by the install-root check instead. Nothing to mark.
        sm = _manager(home, monkeypatch)
        _declare(home, "legacy")
        _converge(sm, home)
        assert sm.read_home_state("legacy") == {}


class TestBlockedAction:
    def test_vanished_home_is_blocked_not_rebuilt(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        app_home = _install_located_app(sm, home)
        (app_home / "data" / "pgdata").write_text("the real cluster")

        # DSM renames the volume root at a cold boot; the app home is gone from
        # every path the platform knows.
        (volumes / "volume6" / "syrviscore").rename(volumes / "volume6" / "syrviscore_1")
        assert not app_home.exists()

        _, plan = _plan(sm, home)
        (action,) = [a for a in plan["actions"] if a["name"] == "immich-db"]
        assert action["kind"] == "blocked"
        assert action["destructive"] is False
        assert str(app_home) in action["message"]
        assert "syrviscore_1" in action["message"]  # names the hypothesis

    def test_apply_never_converts_blocked_into_an_add(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        app_home = _install_located_app(sm, home)
        (volumes / "volume6" / "syrviscore").rename(volumes / "volume6" / "syrviscore_1")

        decls, plan = _plan(sm, home)
        results = services_d.apply_reconcile_plan(sm, decls, plan)

        (row,) = [r for r in results if r["name"] == "immich-db"]
        assert row["kind"] == "blocked"
        assert row["ok"] is False
        # THE assertion: nothing was manufactured at the vanished path.
        assert not app_home.exists()
        assert not (volumes / "volume6" / "syrviscore").exists()
        # ...and the real tree is untouched, waiting to be moved back.
        assert (volumes / "volume6" / "syrviscore_1" / "apps" / "immich-db").is_dir()

    def test_a_blocked_critical_service_fails_the_reconcile(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        _install_located_app(sm, home, critical=True)
        (volumes / "volume6" / "syrviscore").rename(volumes / "volume6" / "syrviscore_1")

        decls, plan = _plan(sm, home)
        results = services_d.apply_reconcile_plan(sm, decls, plan)
        ok, reason = services_d.verdict(plan, results)

        assert ok is False
        assert "immich-db" in reason

    def test_refusal_isolates_other_services(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        _install_located_app(sm, home)
        (volumes / "volume6" / "syrviscore").rename(volumes / "volume6" / "syrviscore_1")
        _declare(home, "unaffected")

        decls, plan = _plan(sm, home)
        results = services_d.apply_reconcile_plan(sm, decls, plan)

        by_name = {r["name"]: r for r in results}
        assert by_name["immich-db"]["ok"] is False
        assert by_name["unaffected"]["ok"] is True

    def test_a_restored_home_converges_normally_again(self, home, volumes, monkeypatch):
        """The guard is a brake, not a latch: move the tree back and it clears."""
        sm = _manager(home, monkeypatch)
        _install_located_app(sm, home)
        (volumes / "volume6" / "syrviscore").rename(volumes / "volume6" / "syrviscore_1")
        _, blocked = _plan(sm, home)
        assert [a["kind"] for a in blocked["actions"]] == ["blocked"]

        (volumes / "volume6" / "syrviscore_1").rename(volumes / "volume6" / "syrviscore")

        _, plan = _plan(sm, home)
        assert plan["actions"] == []
        assert plan["in_sync"] == ["immich-db"]

    def test_a_never_deployed_service_is_never_blocked(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        _declare(home, "brand-new", location="/volume6")
        _, plan = _plan(sm, home)
        assert [a["kind"] for a in plan["actions"]] == ["add"]


class TestEnvFileScaffoldGate:
    """The mechanism that baked EMPTY env into four containers."""

    def _install_with_secret(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        _install_located_app(sm, home, name="db", env_file="secrets.env")
        env = sm._app_home("db") / "secrets" / "secrets.env"
        env.write_text("POSTGRES_PASSWORD=real\n")
        return sm, env

    def test_fresh_install_may_scaffold_an_empty_env_file(self, home, volumes, monkeypatch):
        sm = _manager(home, monkeypatch)
        _install_located_app(sm, home, name="db", env_file="secrets.env")
        # a first install has no secret yet — the operator fills it in after
        assert (sm._app_home("db") / "secrets" / "secrets.env").exists()

    def test_start_refuses_when_the_env_file_vanished(self, home, volumes, monkeypatch):
        sm, env = self._install_with_secret(home, volumes, monkeypatch)
        env.unlink()

        ok, msg = sm.start("db")

        assert ok is False
        assert str(env) in msg
        assert "syrviscore*" in msg  # points at the rename, not at a reinstall
        assert not env.exists(), "refusing must not have created it anyway"

    def test_compose_gen_refuses_directly_for_an_installed_service(
        self, home, volumes, monkeypatch
    ):
        sm, env = self._install_with_secret(home, volumes, monkeypatch)
        shutil.rmtree(sm._app_home("db"))
        from syrviscore.service_schema import load_service_definition

        svc = load_service_definition(home / "services" / "db" / "syrvis-service.yaml")

        with pytest.raises(ServiceValidationError, match="MISSING"):
            sm._generate_compose_file(svc, installed=True)

    def test_recreate_also_fails_closed(self, home, volumes, monkeypatch):
        sm, env = self._install_with_secret(home, volumes, monkeypatch)
        env.unlink()
        ok, msg = sm.recreate("db")
        assert ok is False and "MISSING" in msg


class TestReconcileFloorCheck:
    def test_zero_declarations_against_installed_services_raises(self, home, monkeypatch):
        sm = _manager(home, monkeypatch)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.remove_declaration(home, "app")

        with pytest.raises(services_d.ReconcileError, match="0 declarations but 1 installed"):
            services_d.build_reconcile_plan(sm, {}, [])

    def test_message_points_at_the_install_root_not_at_the_services(self, home, monkeypatch):
        sm = _manager(home, monkeypatch)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.remove_declaration(home, "app")
        with pytest.raises(services_d.ReconcileError) as exc:
            services_d.build_reconcile_plan(sm, {}, [])
        assert "/volume*/syrviscore*" in str(exc.value)

    def test_an_empty_instance_is_still_fine(self, home, monkeypatch):
        sm = _manager(home, monkeypatch)
        plan = services_d.build_reconcile_plan(sm, {}, [])
        assert plan["actions"] == [] and plan["changed"] is False

    def test_an_explicit_prune_policy_is_still_allowed(self, home, monkeypatch):
        """A named policy is an instruction, not an inference — teardown works."""
        sm = _manager(home, monkeypatch)
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.remove_declaration(home, "app")
        plan = services_d.build_reconcile_plan(sm, {}, [], prune="remove")
        assert [a["kind"] for a in plan["actions"]] == ["prune_remove"]
