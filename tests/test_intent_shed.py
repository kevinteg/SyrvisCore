"""Declared service intent: the intent store, the shed overlay, and the guards.

The regression under test is the 2026-08-16 resurrection: fourteen services
were deliberately stopped for a load-shed, a GitOps apply rewrote every
declaration from a repo that still said ``enabled: true``, and the next
converge started them into a seven-day array rebuild. Every class below pins
one link of that chain shut.
"""

import json

import pytest
import yaml
from click.testing import CliRunner

import syrviscore.cli as cli_mod
from syrviscore import guards, intent, services_d
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


def _manager(home):
    return ServiceManager(syrvis_home=home)


def _declare(home, name, image="ghcr.io/a/{}:1.0", **extra):
    d = services_d.get_declarations_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "version": "1.0",
        "image": image.format(name),
        "traefik": {"enabled": True, "subdomain": name, "port": 80, "exposure": "internal"},
    }
    doc.update(extra)
    (d / "{}.yaml".format(name)).write_text(yaml.safe_dump(doc))
    return d / "{}.yaml".format(name)


def _install(home, name):
    """Minimal 'installed' footprint: services/<name>/syrvis-service.yaml."""
    path = home / "services" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "syrvis-service.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0",
                "image": "ghcr.io/a/{}:1.0".format(name),
                "traefik": {
                    "enabled": True,
                    "subdomain": name,
                    "port": 80,
                    "exposure": "internal",
                },
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class TestIntentStore:
    def test_absent_file_is_empty_intent(self, home):
        assert intent.read_intent(home) == {
            "schema_version": 1,
            "device": "in-service",
            "shed": [],
        }
        assert intent.shed_names(home) == set()
        assert not intent.is_shed(home, "anything")

    def test_shed_roundtrips_through_the_file(self, home):
        intent.shed(home, "onyx-api", "md6-resync", until="2026-08-24")
        path = intent.get_intent_path(home)
        assert path.exists()
        raw = json.loads(path.read_text())
        assert raw["schema_version"] == 1
        assert raw["shed"][0]["service"] == "onyx-api"
        assert raw["shed"][0]["reason"] == "md6-resync"
        assert raw["shed"][0]["until"] == "2026-08-24"
        assert raw["shed"][0]["since"].endswith("Z")
        assert intent.is_shed(home, "onyx-api")

    def test_write_is_atomic_and_world_readable(self, home):
        intent.shed(home, "a", "reason")
        path = intent.get_intent_path(home)
        assert oct(path.stat().st_mode)[-3:] == "644"
        assert not list(path.parent.glob("*.tmp"))

    def test_unshed_removes_only_that_row(self, home):
        intent.shed(home, "a", "r1")
        intent.shed(home, "b", "r2")
        removed = intent.unshed(home, "a")
        assert removed["service"] == "a"
        assert intent.shed_names(home) == {"b"}
        assert intent.unshed(home, "a") is None  # idempotent

    def test_re_shedding_keeps_the_original_since(self, home):
        first = intent.shed(home, "a", "r1")
        again = intent.shed(home, "a", "r2", until="2026-09-01")
        assert again["since"] == first["since"]  # "how long has it been down"
        assert again["reason"] == "r2"
        assert len(intent.shed_rows(home)) == 1  # no duplicate row

    def test_garbage_file_degrades_to_empty_never_raises(self, home):
        path = intent.get_intent_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert intent.read_intent(home)["shed"] == []

    def test_one_bad_row_never_erases_the_others(self, home):
        path = intent.get_intent_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "device": "in-service",
                    "shed": [{"nope": 1}, {"service": "b", "reason": "r"}],
                }
            )
        )
        assert intent.shed_names(path.parent) == set()  # wrong dir: sanity
        assert intent.shed_names(home) == {"b"}

    def test_bad_reason_is_refused_on_write(self, home):
        with pytest.raises(intent.IntentError, match="shed reason"):
            intent.shed(home, "a", "because the array is slow")

    def test_bad_until_is_refused_on_write(self, home):
        with pytest.raises(intent.IntentError, match="until"):
            intent.shed(home, "a", "resync", until="next tuesday")

    def test_unknown_device_intent_is_refused_on_write(self, home):
        with pytest.raises(intent.IntentError, match="device intent"):
            intent.write_intent(home, {"device": "asleep", "shed": []})

    def test_expired_rows_are_reported(self, home):
        intent.shed(home, "old", "resync", until="2020-01-01")
        intent.shed(home, "open", "resync")  # no until: never expires
        assert [r["service"] for r in intent.expired(home)] == ["old"]
        assert intent.summary(home)["shed_expired"] == ["old"]

    def test_summary_counts_by_reason(self, home):
        intent.shed(home, "a", "md6-resync")
        intent.shed(home, "b", "md6-resync")
        intent.shed(home, "c", "vendor-outage")
        s = intent.summary(home)
        assert s["shed_count"] == 3
        assert s["shed_reasons"] == {"md6-resync": 2, "vendor-outage": 1}
        assert s["device"] == "in-service" and s["drained"] is False


# ---------------------------------------------------------------------------
# The reconcile overlay
# ---------------------------------------------------------------------------


class TestShedOverlay:
    def _plan(self, home, monkeypatch, status="running"):
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: status)
        monkeypatch.setattr(ServiceManager, "is_service_flapping", lambda self, n: False)
        declarations, invalid = services_d.load_declarations(home)
        return services_d.build_reconcile_plan(_manager(home), declarations, invalid)

    def test_apply_does_not_resurrect_a_shed_service(self, home, monkeypatch):
        """The headline regression: git says enabled: true, the shed says no."""
        _declare(home, "onyx-api", enabled=True)
        _install(home, "onyx-api")
        intent.shed(home, "onyx-api", "md6-resync")

        plan = self._plan(home, monkeypatch, status="stopped")
        assert [a["kind"] for a in plan["actions"]] == []
        assert plan["shed"] == ["onyx-api"]
        assert plan["in_sync"] == [] and plan["disabled"] == []
        assert plan["summary"]["shed"] == 1

    def test_a_shed_service_found_running_is_stopped(self, home, monkeypatch):
        _declare(home, "onyx-api", enabled=True)
        _install(home, "onyx-api")
        intent.shed(home, "onyx-api", "md6-resync")

        plan = self._plan(home, monkeypatch, status="running")
        assert [(a["kind"], a["name"]) for a in plan["actions"]] == [("stop", "onyx-api")]
        assert plan["actions"][0]["shed"] is True
        assert plan["actions"][0]["shed_reason"] == "md6-resync"

    def test_unshed_then_reconcile_starts_it(self, home, monkeypatch):
        _declare(home, "onyx-api", enabled=True)
        _install(home, "onyx-api")
        intent.shed(home, "onyx-api", "md6-resync")
        assert self._plan(home, monkeypatch, status="stopped")["actions"] == []

        intent.unshed(home, "onyx-api")
        plan = self._plan(home, monkeypatch, status="stopped")
        assert [(a["kind"], a["name"]) for a in plan["actions"]] == [("start", "onyx-api")]

    def test_shed_is_reported_apart_from_disabled(self, home, monkeypatch):
        _declare(home, "shed-one", enabled=True)
        _install(home, "shed-one")
        _declare(home, "off-one", enabled=False)
        _install(home, "off-one")
        intent.shed(home, "shed-one", "md6-resync")

        plan = self._plan(home, monkeypatch, status="stopped")
        assert plan["shed"] == ["shed-one"]
        assert plan["disabled"] == ["off-one"]

    def test_shed_service_never_reads_as_l2_drift(self, home, monkeypatch):
        from syrviscore import verify

        _declare(home, "onyx-api", enabled=True)
        _install(home, "onyx-api")
        intent.shed(home, "onyx-api", "md6-resync")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "stopped")
        # The only declared service is shed -> nothing expected -> no report.
        assert verify.gather_l2_drift() is None


# ---------------------------------------------------------------------------
# Bring-up refusals
# ---------------------------------------------------------------------------


class TestBringUpRefusals:
    def test_start_refuses_a_shed_service(self, home, monkeypatch):
        _declare(home, "app")
        _install(home, "app")
        (home / "compose").mkdir(parents=True, exist_ok=True)
        (home / "compose" / "app.yaml").write_text("services: {}\n")
        intent.shed(home, "app", "md6-resync")

        started = []
        monkeypatch.setattr(
            ServiceManager, "_start_service", lambda self, n, p: started.append(n) or (True, "ok")
        )
        ok, msg = _manager(home).start("app")
        assert not ok
        assert "SHED" in msg and "md6-resync" in msg and "unshed" in msg
        assert started == []  # nothing was started

    def test_recreate_refuses_a_shed_service(self, home, monkeypatch):
        _declare(home, "app")
        _install(home, "app")
        (home / "compose").mkdir(parents=True, exist_ok=True)
        (home / "compose" / "app.yaml").write_text("services: {}\n")
        intent.shed(home, "app", "md6-resync")

        recreated = []
        monkeypatch.setattr(
            ServiceManager,
            "_recreate_containers",
            lambda self, n, p: recreated.append(n) or (True, "ok"),
        )
        ok, msg = _manager(home).recreate("app")
        assert not ok and "SHED" in msg
        assert recreated == []

    def test_stop_is_never_refused(self, home, monkeypatch):
        """You must always be able to stop — shed never blocks the down path."""
        _declare(home, "app")
        _install(home, "app")
        (home / "compose").mkdir(parents=True, exist_ok=True)
        (home / "compose" / "app.yaml").write_text("services: {}\n")
        intent.shed(home, "app", "md6-resync")
        monkeypatch.setattr(ServiceManager, "_stop_service", lambda self, n, p, **kw: (True, "ok"))
        assert _manager(home).stop("app")[0]


# ---------------------------------------------------------------------------
# Declaration writes
# ---------------------------------------------------------------------------


class TestDeclarationWrites:
    def test_shed_pins_enabled_false_on_the_dual_write(self, home):
        from syrviscore.service_schema import ServiceDefinition

        _declare(home, "app", enabled=True)
        intent.shed(home, "app", "md6-resync")
        incoming = ServiceDefinition.from_dict(
            {"name": "app", "version": "2", "image": "ghcr.io/a/app:2.0", "enabled": True}
        )
        services_d.write_declaration_from_install(home, incoming)
        written = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert written["image"] == "ghcr.io/a/app:2.0"  # content updated
        assert written["enabled"] is False  # ...intent pinned

    def test_converge_declaration_write_honors_the_shed(self, home):
        """`stack apply --from` is a declaration writer too — the overlay is not
        the bundle path's private rule."""
        from syrviscore import converge

        _declare(home, "app", enabled=True)
        _install(home, "app")
        intent.shed(home, "app", "md6-resync")
        plan = {
            "actions": [{"kind": "declare_update", "name": "app", "destructive": False}],
            "declarations": {
                "app": {
                    "name": "app",
                    "version": "2.0",
                    "image": "ghcr.io/a/app:2.0",
                    "enabled": True,
                }
            },
        }
        converge.apply_plan(plan, manager=_manager(home))
        written = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert written["image"] == "ghcr.io/a/app:2.0"
        assert written["enabled"] is False

    def test_live_disabled_survives_the_dual_write(self, home):
        from syrviscore.service_schema import ServiceDefinition

        _declare(home, "app", enabled=False)
        incoming = ServiceDefinition.from_dict(
            {"name": "app", "version": "2", "image": "ghcr.io/a/app:2.0", "enabled": True}
        )
        services_d.write_declaration_from_install(home, incoming)
        written = yaml.safe_load(services_d.declaration_path(home, "app").read_text())
        assert written["enabled"] is False


# ---------------------------------------------------------------------------
# guard_enable_change
# ---------------------------------------------------------------------------


class TestEnableChangeGuard:
    def test_detects_only_off_to_on(self):
        changes = guards.enable_changes(
            {"a": True, "b": True, "c": False, "d": True},
            {"a": False, "b": True, "c": False, "d": None},
        )
        assert changes == ["a"]  # b already on, c stays off, d is a first write

    def test_shed_services_are_not_enable_changes(self):
        """They are corrected (pinned off), not refused — a different mechanism."""
        assert guards.enable_changes({"a": True}, {"a": False}, shed={"a"}) == []

    def test_refuses_by_name(self):
        with pytest.raises(guards.EnableChangeRefused) as e:
            guards.guard_enable_change(["onyx-api", "immich-server"])
        assert "onyx-api" in str(e.value) and "immich-server" in str(e.value)
        assert "--allow-enable-change" in str(e.value)

    def test_override_is_journaled(self, home):
        guards.guard_enable_change(["onyx-api"], allow=True, home=home, action="apply")
        line = json.loads((home / "logs" / "overrides.log").read_text().strip())
        assert line["action"] == "apply"
        assert line["override"] == "--allow-enable-change"
        assert line["detail"]["services"] == ["onyx-api"]

    def test_no_changes_is_a_no_op(self, home):
        assert guards.guard_enable_change([], allow=False, home=home) == []
        assert not (home / "logs").exists()


# ---------------------------------------------------------------------------
# guard_bulk_degraded
# ---------------------------------------------------------------------------


HEALTHY_MDSTAT = """\
Personalities : [raid1] [raid5]
md2 : active raid5 sata1p3[0] sata2p3[1] sata3p3[2]
      11701305600 blocks super 1.2 level 5, 64k chunk, algorithm 2 [3/3] [UUU]

unused devices: <none>
"""

RESYNC_MDSTAT = """\
Personalities : [raid1] [raid5]
md6 : active raid5 sata1p6[0] sata2p6[1] sata3p6[2] sata4p6[3]
      35103916800 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/4] [UUUU]
      [==>..................]  resync = 12.7% (1489728000/11701305600) \
finish=9241.5min speed=11000K/sec

md2 : active raid1 sata1p2[0] sata2p2[1]
      2097088 blocks [4/2] [UU__]

unused devices: <none>
"""


class TestBulkDegradedGuard:
    def test_healthy_mdstat_reports_nothing(self):
        assert guards.parse_mdstat(HEALTHY_MDSTAT) == []

    def test_resync_is_parsed_with_its_array_and_progress(self):
        rows = guards.parse_mdstat(RESYNC_MDSTAT)
        assert len(rows) == 1
        assert rows[0]["array"] == "md6"
        assert rows[0]["operation"] == "resync"
        assert rows[0]["percent"] == 12.7
        assert rows[0]["speed"] == "11000K/sec"

    def test_recovery_and_check_also_count(self):
        text = "md1 : active raid1 a[0]\n      [>....]  recovery = 1.0% finish=1min speed=1K/sec\n"
        assert guards.parse_mdstat(text)[0]["operation"] == "recovery"
        text = text.replace("recovery", "check")
        assert guards.parse_mdstat(text)[0]["operation"] == "check"

    def test_absent_mdstat_is_not_a_rebuild(self, tmp_path):
        assert guards.busy_arrays(str(tmp_path / "nope")) == []

    def test_refuses_and_names_the_array_and_services(self, home, tmp_path):
        mdstat = tmp_path / "mdstat"
        mdstat.write_text(RESYNC_MDSTAT)
        with pytest.raises(guards.BulkDegradedRefused) as e:
            guards.guard_bulk_degraded(
                "reconcile", home=home, mdstat_path=str(mdstat), services=["onyx-api"]
            )
        msg = str(e.value)
        assert "md6" in msg and "resync" in msg and "onyx-api" in msg and "--force" in msg

    def test_force_proceeds_and_journals(self, home, tmp_path):
        mdstat = tmp_path / "mdstat"
        mdstat.write_text(RESYNC_MDSTAT)
        rows = guards.guard_bulk_degraded(
            "deploy", force=True, home=home, mdstat_path=str(mdstat), services=["vector"]
        )
        assert rows[0]["array"] == "md6"
        line = json.loads((home / "logs" / "overrides.log").read_text().strip())
        assert line["action"] == "deploy" and line["override"] == "--force"
        assert line["detail"]["services"] == ["vector"]

    def test_healthy_array_never_refuses(self, home, tmp_path):
        mdstat = tmp_path / "mdstat"
        mdstat.write_text(HEALTHY_MDSTAT)
        assert guards.guard_bulk_degraded("reconcile", home=home, mdstat_path=str(mdstat)) is None


# ---------------------------------------------------------------------------
# The apply path (instance bundle)
# ---------------------------------------------------------------------------


def _bundle(names_enabled):
    return {
        "apiVersion": "syrvis-instance/v1",
        "declarations": {
            name: {
                "name": name,
                "version": "1.0",
                "image": "ghcr.io/a/{}:1.0".format(name),
                "enabled": enabled,
                "traefik": {
                    "enabled": True,
                    "subdomain": name,
                    "port": 80,
                    "exposure": "internal",
                },
            }
            for name, enabled in names_enabled.items()
        },
    }


class TestApplyIntentGuards:
    def _apply(self, home, doc, **kw):
        from syrviscore.instance_bundle import InstanceBundle, apply_instance_bundle

        return apply_instance_bundle(InstanceBundle.from_dict(doc), home, **kw)

    def test_apply_refuses_to_re_enable_a_declared_off_service(self, home):
        _declare(home, "onyx-api", enabled=False)
        with pytest.raises(guards.EnableChangeRefused) as e:
            self._apply(home, _bundle({"onyx-api": True}))
        assert "onyx-api" in str(e.value)
        # ...and wrote nothing.
        assert (
            yaml.safe_load(services_d.declaration_path(home, "onyx-api").read_text())["enabled"]
            is False
        )

    def test_allow_enable_change_permits_it_and_journals(self, home):
        _declare(home, "onyx-api", enabled=False)
        report = self._apply(home, _bundle({"onyx-api": True}), allow_enable_change=True)
        assert report["declarations"]["enable_changes"] == ["onyx-api"]
        # `enabled: true` is the schema default, so to_dict omits the key.
        written = yaml.safe_load(services_d.declaration_path(home, "onyx-api").read_text())
        assert written.get("enabled", True) is True
        assert (home / "logs" / "overrides.log").exists()

    def test_dry_run_reports_the_resurrection_without_refusing(self, home):
        _declare(home, "onyx-api", enabled=False)
        report = self._apply(home, _bundle({"onyx-api": True}), dry_run=True)
        assert report["declarations"]["enable_changes"] == ["onyx-api"]
        # A plan writes nothing, so the file still says off.
        assert (
            yaml.safe_load(services_d.declaration_path(home, "onyx-api").read_text())["enabled"]
            is False
        )

    def test_shed_service_is_pinned_off_not_refused(self, home):
        _declare(home, "onyx-api", enabled=True)
        intent.shed(home, "onyx-api", "md6-resync")
        report = self._apply(home, _bundle({"onyx-api": True}))
        assert report["declarations"]["shed_pinned"] == ["onyx-api"]
        assert report["declarations"]["enable_changes"] == []
        assert (
            yaml.safe_load(services_d.declaration_path(home, "onyx-api").read_text())["enabled"]
            is False
        )

    def test_a_first_write_is_not_an_enable_change(self, home):
        report = self._apply(home, _bundle({"brand-new": True}))
        assert report["declarations"]["enable_changes"] == []
        assert report["declarations"]["written"] == ["brand-new"]

    def test_an_unparseable_declaration_fails_closed_not_open(self, home):
        """A file this reader cannot parse must not be read as 'enabled: true'
        and silently overwritten as an enable — but it is not an enable CHANGE
        either (we cannot claim it was off). It simply gets rewritten."""
        d = services_d.get_declarations_dir(home)
        d.mkdir(parents=True, exist_ok=True)
        (d / "onyx-api.yaml").write_text("{broken: [")
        report = self._apply(home, _bundle({"onyx-api": True}))
        assert report["declarations"]["enable_changes"] == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestShedCli:
    def test_shed_records_intent_and_stops(self, home, monkeypatch):
        _declare(home, "onyx-api")
        _install(home, "onyx-api")
        (home / "compose").mkdir(parents=True, exist_ok=True)
        (home / "compose" / "onyx-api.yaml").write_text("services: {}\n")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "running")
        stopped = []
        monkeypatch.setattr(
            ServiceManager,
            "_stop_service",
            lambda self, n, p, **kw: stopped.append(n) or (True, "stopped"),
        )
        result = CliRunner().invoke(
            cli,
            [
                "service",
                "shed",
                "--reason",
                "md6-resync",
                "--until",
                "2026-08-24",
                "--json",
                "--",
                "onyx-api",
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["shed"]["reason"] == "md6-resync"
        assert body["shed"]["until"] == "2026-08-24"
        assert stopped == ["onyx-api"]
        # The DECLARATION was not touched — the shed row is the intent now.
        assert "enabled" not in yaml.safe_load(
            services_d.declaration_path(home, "onyx-api").read_text()
        )

    def test_shed_refuses_an_unknown_service(self, home):
        result = CliRunner().invoke(
            cli, ["service", "shed", "--reason", "typo", "--", "no-such-svc"]
        )
        assert result.exit_code != 0
        assert "no such service" in result.output
        assert not intent.get_intent_path(home).exists()

    def test_unshed_lifts_and_starts_nothing(self, home, monkeypatch):
        _declare(home, "onyx-api")
        _install(home, "onyx-api")
        intent.shed(home, "onyx-api", "md6-resync")
        started = []
        monkeypatch.setattr(
            ServiceManager, "_start_service", lambda self, n, p: started.append(n) or (True, "ok")
        )
        result = CliRunner().invoke(cli, ["service", "unshed", "--json", "--", "onyx-api"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["changed"] is True
        assert intent.shed_names(home) == set()
        assert started == []

    def test_unshed_of_an_unshed_service_is_a_no_op(self, home):
        result = CliRunner().invoke(cli, ["service", "unshed", "--json", "--", "whatever"])
        assert result.exit_code == 0
        assert json.loads(result.output)["changed"] is False

    def test_service_list_json_carries_intent(self, home, monkeypatch):
        _declare(home, "shed-one")
        _install(home, "shed-one")
        _declare(home, "off-one", enabled=False)
        _install(home, "off-one")
        _install(home, "undeclared-one")
        intent.shed(home, "shed-one", "md6-resync", until="2026-08-24")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "stopped")

        result = CliRunner().invoke(cli, ["service", "list", "--json"])
        assert result.exit_code == 0, result.output
        rows = {s["name"]: s for s in json.loads(result.output)["services"]}
        assert rows["shed-one"]["intent"] == "shed"
        assert rows["shed-one"]["shed_reason"] == "md6-resync"
        assert rows["shed-one"]["shed_until"] == "2026-08-24"
        assert rows["shed-one"]["declared_enabled"] is True
        assert rows["off-one"]["intent"] == "disabled"
        assert rows["off-one"]["shed_reason"] is None
        assert rows["undeclared-one"]["intent"] == "undeclared"
        assert rows["undeclared-one"]["declared_enabled"] is None

    def test_status_json_carries_the_intent_block(self, home, monkeypatch):
        class FakeDocker:
            def get_container_status(self):
                return {}

        intent.shed(home, "onyx-api", "md6-resync")
        monkeypatch.setattr(cli_mod, "DockerManager", lambda: FakeDocker())
        result = CliRunner().invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        block = json.loads(result.output)["intent"]
        assert block["shed_count"] == 1
        assert block["device"] == "in-service"
        assert block["shed"][0]["service"] == "onyx-api"


# ---------------------------------------------------------------------------
# Seam registry
# ---------------------------------------------------------------------------


class TestSeamRows:
    def test_shed_verbs_are_enumerated_and_mutating(self):
        from syrviscore.seam import registry

        for cmd_id in ("service_shed", "service_shed_until", "service_unshed"):
            cmd = registry.get_command(cmd_id)
            assert cmd.sudo is True
            assert cmd.read_only is False
            assert cmd.destructive is False  # reversible, loses no data
            assert cmd.positional is not None and cmd.positional.kind == "name"

    def test_override_variants_exist_and_are_separate_ids(self):
        from syrviscore.seam import registry

        assert "--force" in registry.get_command("reconcile_force").flags
        assert "--force" in registry.get_command("deploy_force").flags
        assert "--force" not in registry.get_command("reconcile").flags
        assert "--force" not in registry.get_command("deploy").flags
        assert "--allow-enable-change" in registry.get_command("apply_enable_change").flags

    def test_shim_gates_the_new_slots(self):
        from syrviscore.seam.gen import render_shim

        shim = render_shim()
        assert "is_shedreason()" in shim and "is_timestamp()" in shim
        assert '"${5}" = "shed"' in shim  # sudo -n <bin> service shed ...
        assert '"${5}" = "unshed"' in shim

    def test_every_slot_kind_has_a_shim_predicate(self):
        """A kind with no predicate would KeyError at generation time — this is
        the cheap binding that keeps registry and generator in step."""
        from syrviscore.seam import registry
        from syrviscore.seam.gen import _SLOT_PREDICATE

        for cmd in registry.COMMANDS:
            slots = [f.slot for f in cmd.flags if isinstance(f, registry.FlagValue)]
            if cmd.positional is not None:
                slots.append(cmd.positional)
            for slot in slots:
                assert slot.kind in _SLOT_PREDICATE, slot.kind
