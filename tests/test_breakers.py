"""design/60 §11.1 point 6 — ONE breaker store, one union, one reset path.

The arbitration rule added 2026-08-16 for opc:F1/opc:F4: before it, one sick
service could carry two independent open breakers (deploy-plane §11.2 and the
engine's recovery plane 63 §6.2), each counting the same failures, each paging
once for the same fact, each cleared by a DIFFERENT verb. These tests pin the
properties that make that impossible.
"""

import json

import pytest

from syrviscore import breakers


@pytest.fixture
def home(tmp_path):
    (tmp_path / "data" / "state").mkdir(parents=True)
    return tmp_path


DEPLOY = breakers.PLANE_DEPLOY
RECOVERY = breakers.PLANE_RECOVERY


class TestStore:
    def test_it_lives_at_the_specified_path(self, home):
        assert breakers.get_breakers_path(home) == home / "data" / "state" / "breakers.json"

    def test_absent_means_every_breaker_closed(self, home):
        assert breakers.read_breakers(home) == []
        assert breakers.summary(home)["open_count"] == 0
        assert breakers.suppressed_by(home, "romm") is None

    def test_a_row_carries_every_specified_field(self, home):
        row, _ = breakers.record_failure(home, DEPLOY, "deploy-svc:romm", reason="boom", by="cli:x")
        for key in (
            "plane",
            "context",
            "state",
            "consecutive_failures",
            "opened_at",
            "last_probe_at",
            "next_probe_at",
            "reason",
            "by",
        ):
            assert key in row
        raw = json.loads(breakers.get_breakers_path(home).read_text())
        assert raw["schema_version"] == breakers.BREAKERS_SCHEMA_VERSION

    def test_writes_are_atomic_0644_and_leave_no_temp_files(self, home):
        breakers.record_failure(home, DEPLOY, "deploy-svc:romm")
        state = home / "data" / "state"
        assert [p.name for p in state.iterdir() if ".tmp" in p.name] == []
        assert oct(breakers.get_breakers_path(home).stat().st_mode)[-3:] == "644"

    def test_a_garbage_store_degrades_to_all_closed(self, home):
        breakers.get_breakers_path(home).write_text("{ not json")
        assert breakers.read_breakers(home) == []

    def test_bad_rows_are_dropped_individually(self, home):
        breakers.get_breakers_path(home).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "breakers": [
                        {"plane": "nonsense", "context": "x", "state": "open"},
                        "not-a-row",
                        {"plane": "deploy", "context": "deploy-svc:romm", "state": "open"},
                    ],
                }
            )
        )
        rows = breakers.read_breakers(home)
        assert [r["context"] for r in rows] == ["deploy-svc:romm"]

    def test_it_refuses_to_overwrite_a_newer_schema(self, home):
        breakers.get_breakers_path(home).write_text(json.dumps({"schema_version": 99}))
        with pytest.raises(breakers.BreakerError, match="refusing to overwrite"):
            breakers.write_breakers(home, [])

    def test_an_unknown_plane_is_refused(self, home):
        with pytest.raises(breakers.BreakerError, match="unknown plane"):
            breakers.record_failure(home, "imaginary", "x")


class TestTheCurve:
    def test_it_is_exponential_from_the_base(self):
        assert breakers.backoff_seconds(1, base_s=30, jitter=False) == 30
        assert breakers.backoff_seconds(2, base_s=30, jitter=False) == 60
        assert breakers.backoff_seconds(3, base_s=30, jitter=False) == 120

    def test_it_is_capped_at_ten_minutes(self):
        # Doctrine (design/60 §11), not a tuning knob.
        assert breakers.MAX_BACKOFF_S == 600
        assert breakers.backoff_seconds(50, base_s=30, jitter=False) == 600
        for failures in range(1, 40):
            assert breakers.backoff_seconds(failures, base_s=30) <= 600

    def test_jitter_stays_inside_the_cap_and_above_zero(self):
        for _ in range(200):
            delay = breakers.backoff_seconds(4, base_s=30)
            assert 1 <= delay <= 600

    def test_a_huge_failure_count_does_not_explode(self):
        assert breakers.backoff_seconds(10**6, base_s=30, jitter=False) == 600


class TestOpenAndClose:
    def test_it_opens_at_the_threshold_and_pages_exactly_once(self, home):
        context = breakers.deploy_context("romm")
        transitions = []
        for _ in range(6):
            row, opened = breakers.record_failure(home, DEPLOY, context, reason="boom")
            transitions.append(opened)
        # design/60 §11.1 point 4: the OPEN TRANSITION pages exactly once.
        assert transitions.count(True) == 1
        assert transitions[breakers.DEFAULT_THRESHOLD - 1] is True
        assert row["state"] == "open"
        assert row["consecutive_failures"] == 6

    def test_below_the_threshold_it_stays_closed(self, home):
        context = breakers.deploy_context("romm")
        row, opened = breakers.record_failure(home, DEPLOY, context)
        assert row["state"] == "closed" and opened is False

    def test_success_closes_it_and_zeroes_the_count(self, home):
        context = breakers.deploy_context("romm")
        for _ in range(4):
            breakers.record_failure(home, DEPLOY, context)
        row = breakers.record_success(home, DEPLOY, context)
        assert row["state"] == "closed"
        assert row["consecutive_failures"] == 0
        assert row["next_probe_at"] is None

    def test_the_count_persists_ACROSS_calls(self, home):
        # "counted across runs" is implementable only because this store is
        # durable — the journal is per-run and bringup.json is overwritten.
        context = breakers.deploy_context("romm")
        breakers.record_failure(home, DEPLOY, context)
        breakers.record_failure(home, DEPLOY, context)
        assert breakers.get_row(home, DEPLOY, context)["consecutive_failures"] == 2


class TestHalfOpen:
    """§11.2 as AMENDED (opc:F5): the first apply after next_probe_at IS the probe."""

    def _open(self, home, context):
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(
                home, DEPLOY, context, reason="boom", now="2026-08-16T12:00:00Z"
            )

    def test_before_next_probe_at_the_attempt_skips(self, home):
        context = breakers.deploy_context("romm")
        self._open(home, context)
        attempt, half_open = breakers.should_attempt(
            home, DEPLOY, context, now="2026-08-16T12:00:10Z"
        )
        assert attempt is False and half_open is False

    def test_the_first_attempt_after_it_IS_the_half_open_probe(self, home):
        context = breakers.deploy_context("romm")
        self._open(home, context)
        attempt, half_open = breakers.should_attempt(
            home, DEPLOY, context, now="2026-08-16T13:00:00Z"
        )
        assert attempt is True and half_open is True

    def test_an_unrecorded_context_always_attempts(self, home):
        assert breakers.should_attempt(home, DEPLOY, "deploy-svc:new") == (True, False)


class TestCrossPlaneArbitration:
    def test_an_open_breaker_in_ANY_plane_suppresses_the_service(self, home):
        # The recovery plane is open; the deploy plane has never been touched.
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, RECOVERY, breakers.node_context("romm"), reason="crash")
        row = breakers.suppressed_by(home, "romm")
        assert row is not None and row["plane"] == RECOVERY

    def test_a_close_closes_ALL_planes(self, home):
        # ONE reset path: `deploy-stack --only X` and `syrvis up` stop being
        # half-measures that each leave the other's breaker armed.
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, DEPLOY, breakers.deploy_context("romm"))
            breakers.record_failure(home, RECOVERY, breakers.node_context("romm"))
        cleared = breakers.close_service(home, "romm", by="cli:up")
        assert {r["plane"] for r in cleared} == {DEPLOY, RECOVERY}
        assert breakers.suppressed_by(home, "romm") is None
        for plane, context in breakers.service_contexts("romm"):
            assert breakers.get_row(home, plane, context)["consecutive_failures"] == 0

    def test_closing_one_service_leaves_another_alone(self, home):
        for name in ("romm", "onyx-api"):
            for _ in range(breakers.DEFAULT_THRESHOLD):
                breakers.record_failure(home, DEPLOY, breakers.deploy_context(name))
        breakers.close_service(home, "romm", by="cli:up")
        assert breakers.suppressed_by(home, "romm") is None
        assert breakers.suppressed_by(home, "onyx-api") is not None

    def test_closing_a_service_with_nothing_open_is_a_no_op(self, home):
        assert breakers.close_service(home, "romm", by="cli:up") == []

    def test_the_union_view_is_a_concatenation(self, home):
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, DEPLOY, breakers.deploy_context("romm"))
            breakers.record_failure(home, RECOVERY, breakers.node_context("onyx-api"))
        summary = breakers.summary(home)
        assert summary["open_count"] == 2
        assert summary["open_by_plane"] == {DEPLOY: 1, RECOVERY: 1}
        # Every row names its own plane, so no join is possible to get wrong.
        assert all("plane" in row for row in summary["breakers"])


class TestByIsAFieldNotADoctrine:
    """opc:F2 — intent cannot be inferred from the verb; only some surfaces close."""

    @pytest.mark.parametrize("by", ["cli:up", "seam:reconcile", "mcp:deploy"])
    def test_operator_surfaces_close(self, by):
        assert breakers.closes_breakers(by) is True

    @pytest.mark.parametrize("by", ["hostd", "s99", "cron", "", None, "unknown"])
    def test_machine_surfaces_inherit(self, by):
        # Fails SAFE toward NOT resetting: a boot is s99/hostd, so a fresh
        # circuit at boot is no longer automatic.
        assert breakers.closes_breakers(by) is False

    def test_the_row_records_which_surface_touched_it(self, home):
        context = breakers.deploy_context("romm")
        breakers.record_failure(home, DEPLOY, context, by="hostd")
        assert breakers.get_row(home, DEPLOY, context)["by"] == "hostd"


class TestGuardIntegration:
    def test_force_closes_the_breakers_in_scope(self, home, tmp_path):
        from syrviscore import guards

        mdstat = tmp_path / "mdstat"
        mdstat.write_text(
            "md6 : active raid5 sata1p3[0]\n      [==>....]  resync = 12.7% finish=99.9min speed=11000K/sec\n"
        )
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, DEPLOY, breakers.deploy_context("romm"))
        assert breakers.suppressed_by(home, "romm") is not None
        arrays = guards.guard_bulk_degraded(
            "reconcile", force=True, home=home, mdstat_path=str(mdstat), services=["romm"]
        )
        assert arrays  # the array really was busy
        assert breakers.suppressed_by(home, "romm") is None

    def test_a_cron_force_does_NOT_close_them(self, home, tmp_path):
        from syrviscore import guards

        mdstat = tmp_path / "mdstat"
        mdstat.write_text(
            "md6 : active raid5 sata1p3[0]\n      [==>....]  resync = 12.7% finish=99.9min speed=11000K/sec\n"
        )
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, DEPLOY, breakers.deploy_context("romm"))
        guards.guard_bulk_degraded(
            "reconcile",
            force=True,
            home=home,
            mdstat_path=str(mdstat),
            services=["romm"],
            by="cron",
        )
        assert breakers.suppressed_by(home, "romm") is not None

    def test_the_override_journal_records_what_was_closed(self, home, tmp_path):
        from syrviscore import guards

        mdstat = tmp_path / "mdstat"
        mdstat.write_text(
            "md6 : active raid5 sata1p3[0]\n      [==>....]  resync = 12.7% finish=99.9min speed=11000K/sec\n"
        )
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, DEPLOY, breakers.deploy_context("romm"))
        guards.guard_bulk_degraded(
            "reconcile", force=True, home=home, mdstat_path=str(mdstat), services=["romm"]
        )
        line = json.loads((home / "logs" / "overrides.log").read_text().strip().splitlines()[-1])
        assert line["detail"]["breakers_closed"] == ["deploy-svc:romm"]
