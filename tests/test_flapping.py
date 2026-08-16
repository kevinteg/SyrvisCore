"""Crash-loop visibility (incident 2026-08-16, P2).

``syrvis status`` and the reconcile planner were structurally blind to crash
loops — not because a display field was missing, but because both consume a raw
Docker state string that a ``restart: unless-stopped`` container reports as
"running" in the window between two crashes. Six services crash-looped for ~15
minutes while the platform called them running and in-sync; detection came only
from an external alert, 13-18 minutes late by construction.

``RestartCount``, ``StartedAt`` and ``Health`` were on the same object the whole
time and were never read.
"""

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from syrviscore import services_d
from syrviscore.service_manager import FLAP_WINDOW_S, ServiceManager, is_flapping

from conftest import stamp_install_root

NOW = datetime(2026, 8, 16, 5, 40, 0, tzinfo=timezone.utc)


def _ago(seconds):
    return (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class TestIsFlappingPredicate:
    def test_restarted_moments_ago_is_flapping(self):
        assert is_flapping(7, _ago(5), now=NOW) is True

    def test_restarted_but_long_settled_is_not(self):
        assert is_flapping(7, _ago(FLAP_WINDOW_S + 60), now=NOW) is False

    def test_never_restarted_is_not_flapping_however_new(self):
        assert is_flapping(0, _ago(1), now=NOW) is False

    def test_boundary_is_inclusive(self):
        assert is_flapping(1, _ago(FLAP_WINDOW_S), now=NOW) is True
        assert is_flapping(1, _ago(FLAP_WINDOW_S + 1), now=NOW) is False

    @pytest.mark.parametrize(
        "count,started",
        [
            (None, _ago(5)),
            ("", _ago(5)),
            (3, None),
            (3, ""),
            (3, "0001-01-01T00:00:00Z"),  # docker's "never started"
            (3, "not-a-timestamp"),
        ],
    )
    def test_missing_or_unparseable_state_is_never_flapping(self, count, started):
        assert is_flapping(count, started, now=NOW) is False

    def test_nanosecond_precision_is_parsed(self):
        """fromisoformat rejects 9 fractional digits on 3.8 — a naive parse
        would silently drop EVERY docker timestamp and never flag anything."""
        assert is_flapping(1, "2026-08-16T05:39:58.123456789Z", now=NOW) is True

    def test_a_clock_skewed_future_start_is_not_flapping(self):
        future = (NOW + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        assert is_flapping(1, future, now=NOW) is False


class _FakeContainer:
    def __init__(self, state):
        self.attrs = {"State": state}
        self.status = state.get("Status", "running")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    stamp_install_root(h)
    monkeypatch.setenv("DOMAIN", "example.com")
    return h


def _stub_docker(monkeypatch, state):
    """Make ``_container_health``'s lazy docker read return ``state``."""
    import sys
    import types

    fake = types.ModuleType("docker")

    class _Errors:
        class NotFound(Exception):
            pass

    fake.errors = _Errors
    fake.from_env = lambda: types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: _FakeContainer(state))
    )
    monkeypatch.setitem(sys.modules, "docker", fake)


class TestContainerHealth:
    def test_reports_the_full_state_and_synthesizes_flapping(self, home, monkeypatch):
        sm = ServiceManager(syrvis_home=home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        _stub_docker(
            monkeypatch,
            {
                "RestartCount": 5,
                "StartedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "Health": {"Status": "unhealthy"},
            },
        )

        health = sm._container_health("immich_postgres")

        assert health["status"] == "running"  # docker's word is preserved
        assert health["restart_count"] == 5
        assert health["health"] == "unhealthy"
        assert health["flapping"] is True

    def test_a_steady_container_is_not_flapping(self, home, monkeypatch):
        sm = ServiceManager(syrvis_home=home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        _stub_docker(monkeypatch, {"RestartCount": 0, "StartedAt": "2026-08-01T00:00:00.000Z"})
        assert sm._container_health("vm")["flapping"] is False

    def test_unreachable_daemon_degrades_to_status_only(self, home, monkeypatch):
        sm = ServiceManager(syrvis_home=home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "unknown")
        health = sm._container_health("whatever")
        assert health == {
            "status": "unknown",
            "restart_count": None,
            "started_at": None,
            "health": None,
            "flapping": False,
        }


class TestSurfacedInListServices:
    def test_rows_carry_the_flap_fields(self, home, monkeypatch):
        sm = ServiceManager(syrvis_home=home)
        sm._reload_traefik = lambda: None
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        _stub_docker(
            monkeypatch,
            {
                "RestartCount": 4,
                "StartedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "Health": {"Status": "starting"},
            },
        )

        (row,) = sm.list()

        assert row["status"] == "running"
        assert row["flapping"] is True
        assert row["restart_count"] == 4
        assert row["docker_health"] == "starting"


class TestPlannerTreatsFlappingAsNotInSync:
    def _setup(self, home):
        sm = ServiceManager(syrvis_home=home)
        sm._reload_traefik = lambda: None
        assert sm.add_image("app", "ghcr.io/a/app:1.0", start=False)[0]
        services_d.adopt(sm, "app")
        return sm

    def test_running_but_flapping_plans_a_start(self, home, monkeypatch):
        sm = self._setup(home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        monkeypatch.setattr(ServiceManager, "is_service_flapping", lambda self, n: True)

        decls, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, decls, invalid)

        assert [a["kind"] for a in plan["actions"]] == ["start"]
        assert plan["actions"][0]["flapping"] is True
        assert plan["in_sync"] == []

    def test_running_and_stable_is_still_in_sync(self, home, monkeypatch):
        sm = self._setup(home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        monkeypatch.setattr(ServiceManager, "is_service_flapping", lambda self, n: False)

        decls, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, decls, invalid)

        assert plan["actions"] == []
        assert plan["in_sync"] == ["app"]

    def test_flapping_is_probed_by_container_name_not_service_name(self, home, monkeypatch):
        """immich-db.yaml declares container_name: immich_postgres — joining on
        the wrong one silently probes a container that does not exist."""
        sm = ServiceManager(syrvis_home=home)
        sm._reload_traefik = lambda: None
        d = services_d.get_declarations_dir(home)
        d.mkdir(parents=True, exist_ok=True)
        (d / "immich-db.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "immich-db",
                    "version": "1.0",
                    "image": "ghcr.io/a/db:1.0",
                    "container_name": "immich_postgres",
                }
            )
        )
        sm._start_service = lambda n, cp: (True, "started")
        decls, invalid = services_d.load_declarations(home)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "stopped")
        services_d.apply_reconcile_plan(
            sm, decls, services_d.build_reconcile_plan(sm, decls, invalid)
        )

        probed = []
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        monkeypatch.setattr(
            ServiceManager,
            "is_service_flapping",
            lambda self, n: probed.append(n) or False,
        )
        services_d.build_reconcile_plan(sm, decls, invalid)

        assert probed == ["immich_postgres"]


class TestCoreStatusCarriesIt:
    def test_get_container_status_reports_restarts_and_flapping(self, monkeypatch):
        from syrviscore import docker_manager

        mgr = docker_manager.DockerManager.__new__(docker_manager.DockerManager)
        container = _FakeContainer(
            {
                "RestartCount": 9,
                "StartedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
            }
        )
        container.name = "traefik"
        container.labels = {"com.docker.compose.service": "traefik"}
        container.attrs["Created"] = "2026-08-16T05:00:00.000000000Z"
        container.attrs["Config"] = {"Image": "traefik:v3.7.10"}
        monkeypatch.setattr(
            docker_manager.DockerManager, "get_core_containers", lambda self: [container]
        )

        out = mgr.get_container_status()

        assert out["traefik"]["restart_count"] == 9
        assert out["traefik"]["flapping"] is True
        assert out["traefik"]["status"] == "running"
