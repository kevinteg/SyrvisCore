"""design/63 M1 — `depends_on` schema, graph validation and topological ordering.

M1 is deliberately ORDERING ONLY: no readiness gates, no waits, no engine (that
is M2). What is asserted here is the schema, the four whole-set validation rules
of D2 (as AMENDED 2026-08-16 for opc:F10), the plan-time `blocked` bucket, and
that the graph orders the plan with SC-B's band key as the tie-breaker.
"""

import pytest
import yaml

import syrviscore.cli as cli_mod
from syrviscore import services_d
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import (
    MAX_DEPENDS_ON,
    ServiceDefinition,
    ServiceValidationError,
    parse_dependency_entry,
)

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


def _declare(home, name, **extra):
    d = services_d.get_declarations_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"name": name, "version": "1.0", "image": "ghcr.io/a/{}:1.0".format(name)}
    doc.update(extra)
    (d / "{}.yaml".format(name)).write_text(yaml.safe_dump(doc))


def _defn(name, **extra):
    doc = {"name": name, "version": "1", "image": "ghcr.io/a/{}:1.0".format(name)}
    doc.update(extra)
    return ServiceDefinition.from_dict(doc)


HEALTHCHECK = {"test": ["CMD", "true"], "interval": "10s"}


# ---------------------------------------------------------------------------
# D1 — the edge form
# ---------------------------------------------------------------------------


class TestEdgeForm:
    def test_the_three_ratified_readiness_classes(self):
        svc = _defn(
            "romm",
            depends_on=["romm-db:healthy", "romm-valkey", "immich-machine-learning:soft"],
        )
        assert svc.dependency_edges() == [
            {"on": "romm-db", "readiness": "healthy"},
            {"on": "romm-valkey", "readiness": "started"},
            {"on": "immich-machine-learning", "readiness": "soft"},
        ]

    def test_parse_order_splits_the_suffix_before_name_validation(self):
        # design/63 D1: `romm-db:healthy` fails NAME_RE as written, so a parse
        # that validated the name first would refuse every readiness edge on its
        # first day. This is the regression test for that exact ordering.
        assert parse_dependency_entry("romm-db:healthy") == {
            "on": "romm-db",
            "readiness": "healthy",
        }

    def test_an_unknown_suffix_is_an_error_not_a_default(self):
        # No silent downgrade — the syntax half of the same principle that makes
        # `healthy`-without-a-healthcheck an error.
        with pytest.raises(ServiceValidationError, match="Invalid readiness 'helthy'"):
            _defn("a", depends_on=["db:helthy"])

    def test_an_empty_suffix_is_an_error(self):
        with pytest.raises(ServiceValidationError, match="empty readiness"):
            _defn("a", depends_on=["db:"])

    @pytest.mark.parametrize("entry", ["", "DB", "db/../x", "db:healthy:extra", 42, None])
    def test_malformed_entries_are_refused(self, entry):
        with pytest.raises(ServiceValidationError):
            _defn("a", depends_on=[entry])

    def test_a_reserved_core_name_is_still_reserved_as_a_target(self):
        with pytest.raises(ServiceValidationError, match="reserved"):
            _defn("a", depends_on=["traefik"])

    def test_self_edges_are_refused(self):
        with pytest.raises(ServiceValidationError, match="depend on itself"):
            _defn("onyx-api", depends_on=["onyx-api"])

    def test_duplicate_targets_are_refused(self):
        with pytest.raises(ServiceValidationError, match="twice"):
            _defn("a", depends_on=["db", "db:healthy"])

    def test_the_edge_count_is_bounded(self):
        ok = ["dep{}".format(i) for i in range(MAX_DEPENDS_ON)]
        assert len(_defn("a", depends_on=ok).depends_on) == MAX_DEPENDS_ON
        with pytest.raises(ServiceValidationError, match="at most"):
            _defn("a", depends_on=ok + ["one-too-many"])

    def test_entries_round_trip_verbatim_through_to_dict(self):
        # A declaration must survive install -> manifest -> reconcile diff
        # without churning: a normalized spelling would plan a spurious replace.
        edges = ["db:healthy", "cache", "ml:soft"]
        svc = _defn("a", depends_on=edges)
        assert svc.to_dict()["depends_on"] == edges
        assert ServiceDefinition.from_dict(svc.to_dict()).depends_on == edges

    def test_absent_depends_on_is_omitted_entirely(self):
        assert "depends_on" not in _defn("a").to_dict()


class TestNeverEmittedIntoCompose:
    def test_the_generated_compose_carries_no_depends_on(self, home):
        # design/63 D1: the key is REINTERPRETED at the orchestration layer and
        # never emitted — compose depends_on still cannot cross single-service
        # projects, and writing one would fail at docker-run time.
        sm = ServiceManager(syrvis_home=home)
        (home / "compose").mkdir(parents=True, exist_ok=True)
        (home / "services").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        path = sm._generate_compose_file(_defn("onyx-api", depends_on=["db:healthy"]))
        compose = yaml.safe_load(path.read_text())
        assert "depends_on" not in compose["services"]["onyx-api"]
        assert "depends_on" not in path.read_text()


# ---------------------------------------------------------------------------
# D2 — whole-set validation
# ---------------------------------------------------------------------------


class TestGraphValidation:
    def test_an_edge_onto_a_nonexistent_service_invalidates_the_declaring_file(self, home):
        _declare(home, "onyx-api", depends_on=["onyx-relational-db"])
        _declare(home, "bystander")
        valid, invalid = services_d.load_declarations(home)
        # Isolation: only the DECLARING file goes invalid.
        assert set(valid) == {"bystander"}
        assert [row["file"] for row in invalid] == ["onyx-api.yaml"]
        assert "not declared on this instance" in invalid[0]["error"]

    def test_a_healthy_edge_onto_a_checkless_target_is_a_validation_error(self, home):
        # dep:F9 / design/63 D1: never a silent downgrade to `started`.
        _declare(home, "romm", depends_on=["romm-db:healthy"])
        _declare(home, "romm-db")  # no healthcheck:
        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"romm-db"}
        assert "declares no healthcheck" in invalid[0]["error"]

    def test_a_healthy_edge_onto_a_checked_target_is_fine(self, home):
        _declare(home, "romm", depends_on=["romm-db:healthy"])
        _declare(home, "romm-db", healthcheck=HEALTHCHECK)
        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"romm", "romm-db"}
        assert invalid == []

    def test_a_started_edge_onto_a_checkless_target_is_fine(self):
        # The honest spelling the error message tells the author to use.
        graph = services_d.build_dependency_graph(
            {"romm": _defn("romm", depends_on=["romm-db"]), "romm-db": _defn("romm-db")}
        )
        assert graph["errors"] == {}

    def test_a_cycle_invalidates_every_member_and_names_the_ring(self, home):
        _declare(home, "a", depends_on=["b"])
        _declare(home, "b", depends_on=["c"])
        _declare(home, "c", depends_on=["a"])
        _declare(home, "innocent")
        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"innocent"}
        assert {row["file"] for row in invalid} == {"a.yaml", "b.yaml", "c.yaml"}
        for row in invalid:
            assert "dependency cycle" in row["error"]
            assert "->" in row["error"]

    def test_a_two_node_cycle_is_caught(self):
        graph = services_d.build_dependency_graph(
            {"a": _defn("a", depends_on=["b"]), "b": _defn("b", depends_on=["a"])}
        )
        assert set(graph["errors"]) == {"a", "b"}

    def test_a_soft_cycle_is_still_a_cycle(self):
        # A soft edge still ORDERS, so a soft ring leaves the topological order
        # undefined — it is refused for the same reason a hard one is.
        graph = services_d.build_dependency_graph(
            {"a": _defn("a", depends_on=["b:soft"]), "b": _defn("b", depends_on=["a:soft"])}
        )
        assert set(graph["errors"]) == {"a", "b"}

    def test_a_diamond_is_not_a_cycle(self):
        graph = services_d.build_dependency_graph(
            {
                "top": _defn("top", depends_on=["left", "right"]),
                "left": _defn("left", depends_on=["base"]),
                "right": _defn("right", depends_on=["base"]),
                "base": _defn("base"),
            }
        )
        assert graph["errors"] == {}
        assert graph["depths"] == {"base": 0, "left": 1, "right": 1, "top": 2}

    def test_an_edge_onto_an_INVALID_file_blocks_rather_than_invalidates(self, home):
        # design/63 D2: unknown != broken. A target whose own file is invalid is
        # a plan-time block on the dependant, not a second invalid file.
        _declare(home, "consumer", depends_on=["store"])
        d = services_d.get_declarations_dir(home)
        (d / "store.yaml").write_text("{ not: [valid")
        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"consumer"}
        assert [row["file"] for row in invalid] == ["store.yaml"]

    def test_zero_edges_anywhere_is_a_no_op_pass(self, home):
        _declare(home, "a")
        _declare(home, "b")
        valid, invalid = services_d.load_declarations(home)
        assert set(valid) == {"a", "b"} and invalid == []


class TestDepths:
    def test_a_chain_of_four_gets_four_waves(self):
        graph = services_d.build_dependency_graph(
            {
                "onyx-nginx": _defn("onyx-nginx", depends_on=["onyx-web"]),
                "onyx-web": _defn("onyx-web", depends_on=["onyx-api"]),
                "onyx-api": _defn("onyx-api", depends_on=["onyx-relational-db"]),
                "onyx-relational-db": _defn("onyx-relational-db"),
            }
        )
        assert graph["depths"]["onyx-relational-db"] == 0
        assert graph["depths"]["onyx-nginx"] == 3
        assert graph["waves"] == [
            ["onyx-relational-db"],
            ["onyx-api"],
            ["onyx-web"],
            ["onyx-nginx"],
        ]

    def test_longest_path_wins_so_a_shortcut_never_reorders(self):
        # top -> mid -> base AND top -> base: top must still land after mid.
        graph = services_d.build_dependency_graph(
            {
                "top": _defn("top", depends_on=["mid", "base"]),
                "mid": _defn("mid", depends_on=["base"]),
                "base": _defn("base"),
            }
        )
        assert graph["depths"] == {"base": 0, "mid": 1, "top": 2}


# ---------------------------------------------------------------------------
# D3 — ordering, and the blocked bucket
# ---------------------------------------------------------------------------


def _install(sm, name, **extra):
    ok, msg = sm.install_declaration(_defn(name, **extra), start=False)
    assert ok, msg


class TestTopologicalOrdering:
    def _plan(self, home, monkeypatch, decls, status="stopped"):
        sm = ServiceManager(syrvis_home=home)
        for name, extra in decls.items():
            _install(sm, name, **extra)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: status)
        declarations, invalid = services_d.load_declarations(home)
        return services_d.build_reconcile_plan(sm, declarations, invalid)

    def test_the_graph_beats_the_band_when_they_disagree(self, home, monkeypatch):
        # design/63 D6: "where an edge and a band disagree, the EDGE wins".
        # grafana (band 50) declares an edge onto vmalert (band 20); the band
        # ordering alone would start grafana FIRST.
        plan = self._plan(
            home,
            monkeypatch,
            {
                "grafana": {"shutdown": {"priority": 50}, "depends_on": ["vmalert"]},
                "vmalert": {"shutdown": {"priority": 20}},
            },
        )
        assert [a["name"] for a in plan["actions"]] == ["vmalert", "grafana"]

    def test_intra_band_chains_order_correctly(self, home, monkeypatch):
        # The capability bands provably cannot express: three services, one
        # band, a real chain (design/63 D1's rejected-alternative pricing).
        plan = self._plan(
            home,
            monkeypatch,
            {
                "onyx-nginx": {"shutdown": {"priority": 20}, "depends_on": ["onyx-web"]},
                "onyx-web": {"shutdown": {"priority": 20}, "depends_on": ["onyx-api"]},
                "onyx-api": {"shutdown": {"priority": 20}},
            },
        )
        assert [a["name"] for a in plan["actions"]] == ["onyx-api", "onyx-web", "onyx-nginx"]

    def test_the_band_is_the_tie_breaker_inside_a_wave(self, home, monkeypatch):
        # Two edge-free stores at the same depth: SC-B's reversed-band ordering
        # still decides, which is what "tie-breaker, not a second sort" means.
        plan = self._plan(
            home,
            monkeypatch,
            {
                "consumer": {"shutdown": {"priority": 20}, "depends_on": ["store-a"]},
                "store-a": {"shutdown": {"priority": 90}},
                "store-b": {"shutdown": {"priority": 70}},
            },
        )
        # wave 0 = {store-a, store-b} ordered by DESCENDING band; then consumer.
        assert [a["name"] for a in plan["actions"]] == ["store-a", "store-b", "consumer"]

    def test_no_edges_anywhere_orders_exactly_as_the_band_only_interim_did(self, home, monkeypatch):
        plan = self._plan(
            home,
            monkeypatch,
            {
                "onyx-api": {"shutdown": {"priority": 20}},
                "onyx-relational-db": {"shutdown": {"priority": 90}},
                "onyx-redis": {"shutdown": {"priority": 70}},
            },
        )
        assert [a["name"] for a in plan["actions"]] == [
            "onyx-relational-db",
            "onyx-redis",
            "onyx-api",
        ]

    def test_bring_down_is_the_exact_reverse(self, home, monkeypatch):
        # design/63 D6: reverse topological — dependents drain before stores.
        plan = self._plan(
            home,
            monkeypatch,
            {
                "consumer": {"depends_on": ["store"], "enabled": False},
                "store": {"enabled": False},
            },
            status="running",
        )
        assert [(a["kind"], a["name"]) for a in plan["actions"]] == [
            ("stop", "consumer"),
            ("stop", "store"),
        ]

    def test_soft_edges_order_too(self, home, monkeypatch):
        plan = self._plan(
            home,
            monkeypatch,
            {"alertmanager": {"depends_on": ["ntfy-alertmanager:soft"]}, "ntfy-alertmanager": {}},
        )
        assert [a["name"] for a in plan["actions"]] == ["ntfy-alertmanager", "alertmanager"]

    def test_the_plan_exposes_the_solved_graph(self, home, monkeypatch):
        plan = self._plan(home, monkeypatch, {"consumer": {"depends_on": ["store"]}, "store": {}})
        assert plan["graph"]["depths"] == {"consumer": 1, "store": 0}
        assert plan["graph"]["waves"] == [["store"], ["consumer"]]


class TestBlockedByDependency:
    """D2 as AMENDED 2026-08-16 (opc:F10): plan-time blocked, never invalid."""

    def _plan(self, home, monkeypatch, decls, status="stopped"):
        sm = ServiceManager(syrvis_home=home)
        for name, extra in decls.items():
            _install(sm, name, **extra)
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: status)
        declarations, invalid = services_d.load_declarations(home)
        return sm, services_d.build_reconcile_plan(sm, declarations, invalid)

    def test_a_hard_edge_onto_a_DISABLED_target_blocks_and_stays_valid(self, home, monkeypatch):
        _, plan = self._plan(
            home, monkeypatch, {"consumer": {"depends_on": ["store"]}, "store": {"enabled": False}}
        )
        # The declaration is VALID — this is the whole point of the demotion.
        assert plan["invalid"] == []
        assert [r["name"] for r in plan["blocked"]] == ["consumer"]
        assert plan["blocked"][0]["reason"] == "blocked (dependency store disabled)"
        assert plan["blocked"][0]["withheld"] == "start"
        assert plan["summary"]["blocked"] == 1
        # ...and no start was planned for it.
        assert [a["name"] for a in plan["actions"]] == []

    def test_a_hard_edge_onto_a_SHED_target_blocks(self, home, monkeypatch):
        from syrviscore import intent as intent_mod

        intent_mod.shed(home, "store", reason="md6-resync")
        _, plan = self._plan(
            home, monkeypatch, {"consumer": {"depends_on": ["store"]}, "store": {}}
        )
        assert plan["invalid"] == []
        assert plan["blocked"][0]["reason"] == "blocked (dependency store shed)"
        assert plan["blocked"][0]["why"] == "shed"

    def test_blocked_is_NOT_a_failure(self, home, monkeypatch):
        # A 14-service load-shed must not fail every hourly reconcile for every
        # dependant in those subtrees — the exact reasoning that made `terminal`
        # a bucket rather than an action.
        sm, plan = self._plan(
            home,
            monkeypatch,
            {"consumer": {"depends_on": ["store"], "critical": True}, "store": {"enabled": False}},
        )
        declarations, _ = services_d.load_declarations(home)
        results = services_d.apply_reconcile_plan(sm, declarations, plan)
        assert results == []
        assert services_d.verdict(plan, results)[0] is True

    def test_a_SOFT_edge_never_blocks(self, home, monkeypatch):
        # "order when cheap, never gate" — the whole distinction the class carries.
        _, plan = self._plan(
            home,
            monkeypatch,
            {
                "immich-server": {"depends_on": ["immich-machine-learning:soft"]},
                "immich-machine-learning": {"enabled": False},
            },
        )
        assert plan["blocked"] == []
        assert [a["name"] for a in plan["actions"]] == ["immich-server"]

    def test_blocking_is_transitive_over_hard_edges(self, home, monkeypatch):
        _, plan = self._plan(
            home,
            monkeypatch,
            {
                "top": {"depends_on": ["mid"]},
                "mid": {"depends_on": ["base"]},
                "base": {"enabled": False},
            },
        )
        rows = {r["name"]: r for r in plan["blocked"]}
        assert set(rows) == {"top", "mid"}
        assert rows["mid"]["why"] == "disabled"
        assert rows["top"]["why"] == "blocked"  # named its immediate dependency
        assert rows["top"]["dependency"] == "mid"

    def test_an_independent_branch_still_converges(self, home, monkeypatch):
        _, plan = self._plan(
            home,
            monkeypatch,
            {
                "consumer": {"depends_on": ["store"]},
                "store": {"enabled": False},
                "unrelated": {},
            },
        )
        assert [a["name"] for a in plan["actions"]] == ["unrelated"]
        assert [r["name"] for r in plan["blocked"]] == ["consumer"]

    def test_a_RUNNING_blocked_service_is_left_alone_not_torn_down(self, home, monkeypatch):
        # Only BRING-UP is withheld. A service that is already serving must not
        # be stopped because its dependency was shed.
        _, plan = self._plan(
            home,
            monkeypatch,
            {"consumer": {"depends_on": ["store"]}, "store": {"enabled": False}},
            status="running",
        )
        assert plan["blocked"] == []
        assert "consumer" in plan["in_sync"]

    def test_a_stop_is_never_withheld(self, home, monkeypatch):
        # A service on its way DOWN does not need its dependency.
        _, plan = self._plan(
            home,
            monkeypatch,
            {
                "consumer": {"depends_on": ["store"], "enabled": False},
                "store": {"enabled": False},
            },
            status="running",
        )
        assert [(a["kind"], a["name"]) for a in plan["actions"]] == [
            ("stop", "consumer"),
            ("stop", "store"),
        ]
        assert plan["blocked"] == []

    def test_an_edge_onto_an_invalid_file_blocks_the_dependant(self, home, monkeypatch):
        sm = ServiceManager(syrvis_home=home)
        _install(sm, "consumer", depends_on=["store"])
        d = services_d.get_declarations_dir(home)
        (d / "store.yaml").write_text("{ not: [valid")
        monkeypatch.setattr(ServiceManager, "_get_service_status", lambda self, n: "stopped")
        declarations, invalid = services_d.load_declarations(home)
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        assert plan["blocked"][0]["why"] == "invalid"
        assert plan["blocked"][0]["reason"] == "blocked (dependency store invalid)"


class TestBlockedRendering:
    def test_the_cli_prints_the_blocked_bucket(self, home, monkeypatch, capsys):
        plan = {
            "summary": {"declared": 2, "invalid": 0},
            "in_sync": [],
            "disabled": [],
            "shed": [],
            "terminal": [],
            "blocked": [
                {
                    "name": "consumer",
                    "withheld": "start",
                    "dependency": "store",
                    "why": "shed",
                    "reason": "blocked (dependency store shed)",
                }
            ],
            "unmanaged": [],
            "invalid": [],
            "actions": [],
        }
        cli_mod._render_reconcile_plan(plan)
        out = capsys.readouterr().out
        assert "blocked: 1" in out
        assert "blocked (dependency store shed)" in out
        assert "consumer" in out
