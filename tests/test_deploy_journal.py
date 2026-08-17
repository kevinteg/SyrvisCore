"""design/60 §5 D6 — the deploy-journal contract, and §11.1 point 6's store.

D6 is normative in four clauses (canonical path, required schema_version with
unknown => refuse to act, the enumerated terminal set with `failed` TERMINAL,
and the 60-minute staleness rule). Each has a test here, because 63 M2 lists
this contract as a shipping prerequisite and will consume it verbatim.
"""

import json
import os

import pytest

from syrviscore import breakers, deploy_journal as dj


@pytest.fixture
def home(tmp_path):
    (tmp_path / "data" / "state").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# D6 clause 1 — the canonical path
# ---------------------------------------------------------------------------


class TestCanonicalPath:
    def test_it_lives_beside_the_other_state_files(self, home):
        path = dj.get_journal_path(home)
        assert path == home / "data" / "state" / "deploy-journal.json"
        assert path.is_absolute()
        # One state directory, one convention.
        assert path.parent == breakers.get_breakers_path(home).parent

    def test_absent_means_no_run(self, home):
        assert dj.read_journal(home) is None
        assert dj.journal_status(home)["state"] == "absent"
        assert dj.journal_status(home)["act"] is True


# ---------------------------------------------------------------------------
# D6 clause 2 — schema_version, and unknown => refuse to act
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_a_written_journal_carries_the_version(self, home):
        dj.begin_run(home, ["romm"])
        raw = json.loads(dj.get_journal_path(home).read_text())
        assert raw["schema_version"] == dj.JOURNAL_SCHEMA_VERSION
        assert isinstance(raw["schema_version"], int)
        for key in ("run_id", "started_at", "finished_at", "services"):
            assert key in raw

    def test_an_unknown_version_reports_unknown_and_refuses_to_act(self, home):
        dj.get_journal_path(home).write_text(json.dumps({"schema_version": 99, "services": {}}))
        status = dj.journal_status(home)
        assert status["state"] == "unknown"
        assert status["act"] is False
        assert "99" in status["detail"]

    def test_an_unparseable_journal_is_UNKNOWN_never_ABSENT(self, home):
        # "Absent means no run; unparseable means I cannot tell" — different
        # verdicts, and conflating them is what D6 clause 2 forbids.
        dj.get_journal_path(home).write_text("{ this is not json")
        status = dj.journal_status(home)
        assert status["state"] == "unknown"
        assert status["act"] is False

    def test_a_non_mapping_journal_is_unknown(self, home):
        dj.get_journal_path(home).write_text("[1, 2, 3]")
        assert dj.journal_status(home)["state"] == "unknown"

    def test_an_unknown_journal_refuses_nothing_by_intersection(self, home):
        # It refuses to ACT on the file; it does not manufacture a refusal of
        # the caller's own work out of a file it cannot read.
        dj.get_journal_path(home).write_text("{ garbage")
        assert dj.intersects(home, ["romm"]) == []


# ---------------------------------------------------------------------------
# D6 clause 3 — the terminal set
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_the_enumerated_sets(self):
        assert dj.TERMINAL_STATES == {"started", "healthy", "skipped", "failed"}
        assert dj.NON_TERMINAL_STATES == {"pending", "stopping", "starting"}
        assert not (dj.TERMINAL_STATES & dj.NON_TERMINAL_STATES)

    def test_failed_IS_terminal(self, home):
        # design/63 said so and design/60 did not; D6 ruled it. A failed service
        # is a finished decision, not an in-flight one.
        dj.begin_run(home, ["romm"])
        dj.record_event(home, "romm", dj.STATE_FAILED, detail="boom")
        assert dj.journal_status(home)["state"] == "terminal"
        assert dj.in_flight_services(dj.read_journal(home)) == set()

    def test_started_is_terminal_too(self, home):
        # design/60 §3.4 as amended: a checkless service's completed step is
        # `started`, and leaving it non-terminal would make every deploy of one
        # look in-flight forever.
        dj.begin_run(home, ["victoria-logs"])
        dj.record_event(home, "victoria-logs", dj.STATE_STARTED)
        assert dj.journal_status(home)["state"] == "terminal"

    def test_a_pending_row_is_in_flight(self, home):
        dj.begin_run(home, ["romm", "romm-db"])
        status = dj.journal_status(home)
        assert status["state"] == "in-flight"
        assert status["in_flight"] == ["romm", "romm-db"]

    def test_in_flight_needs_a_non_terminal_row_AND_no_finished_at(self, home):
        dj.begin_run(home, ["romm"])
        dj.finish_run(home)
        assert dj.journal_status(home)["state"] == "terminal"

    def test_an_unknown_state_is_refused_on_the_write_path(self, home):
        dj.begin_run(home, ["romm"])
        # record_event never raises (it must not fail a deploy) but it also must
        # not persist a state nothing can interpret.
        assert dj.record_event(home, "romm", "vibing") is None
        assert dj.read_journal(home)["services"]["romm"]["state"] == "pending"


# ---------------------------------------------------------------------------
# D6 clause 4 — staleness
# ---------------------------------------------------------------------------


class TestStaleness:
    def _aged(self, home, seconds):
        dj.begin_run(home, ["romm"])
        record = dj.read_journal(home)
        record["started_at"] = "2026-08-16T00:00:00Z"
        record["pid"] = os.getpid()  # alive, so only the clock decides
        dj._write(home, record)
        return "2026-08-16T{:02d}:{:02d}:00Z".format(seconds // 3600, (seconds % 3600) // 60)

    def test_fresh_in_flight_is_in_flight(self, home):
        now = self._aged(home, 600)  # 10 minutes
        assert dj.journal_status(home, now=now)["state"] == "in-flight"

    def test_past_sixty_minutes_it_is_stale(self, home):
        now = self._aged(home, 3660)  # 61 minutes
        assert dj.STALE_AFTER_S == 3600
        assert dj.journal_status(home, now=now)["state"] == "stale"

    def test_a_dead_pid_is_stale_however_fresh_the_clock(self, home):
        dj.begin_run(home, ["romm"])
        record = dj.read_journal(home)
        record["pid"] = 999999999  # not a live process
        dj._write(home, record)
        assert dj.journal_status(home)["state"] == "stale"

    def test_a_stale_journal_REFUSES_NOTHING(self, home):
        # The failure mode this clause exists to forbid: a stale journal holding
        # unattended bring-up hostage forever.
        now = self._aged(home, 7200)
        assert dj.journal_status(home, now=now)["state"] == "stale"
        assert dj.intersects(home, ["romm"], now=now) == []

    def test_a_FRESH_intersecting_journal_is_what_refuses(self, home):
        now = self._aged(home, 60)
        assert dj.intersects(home, ["romm", "grafana"], now=now) == ["romm"]
        # ...and a non-intersecting bring-up set proceeds.
        assert dj.intersects(home, ["grafana"], now=now) == []

    def test_a_terminal_journal_is_never_stale(self, home):
        dj.begin_run(home, ["romm"])
        dj.record_event(home, "romm", dj.STATE_STARTED)
        assert dj.is_stale(dj.read_journal(home)) is False


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------


class TestJournalMechanics:
    def test_events_append_with_revision(self, home):
        dj.begin_run(home, ["romm"], by="cli:deploy")
        dj.record_event(home, "romm", dj.STATE_STARTING)
        dj.record_event(home, "romm", dj.STATE_STARTED, revision=7)
        record = dj.read_journal(home)
        assert [e["state"] for e in record["events"]] == ["starting", "started"]
        assert record["events"][-1]["revision"] == 7
        assert record["services"]["romm"]["revision"] == 7
        assert record["by"] == "cli:deploy"

    def test_events_are_bounded_oldest_out(self, home):
        dj.begin_run(home, ["romm"])
        for _ in range(dj.MAX_EVENTS + 25):
            dj.record_event(home, "romm", dj.STATE_STARTING)
        record = dj.read_journal(home)
        assert len(record["events"]) == dj.MAX_EVENTS

    def test_writes_are_atomic_and_leave_no_temp_files(self, home):
        dj.begin_run(home, ["romm"])
        dj.record_event(home, "romm", dj.STATE_STARTED)
        leftovers = [p.name for p in (home / "data" / "state").iterdir() if ".tmp" in p.name]
        assert leftovers == []
        assert oct(dj.get_journal_path(home).stat().st_mode)[-3:] == "644"

    def test_an_event_with_no_open_run_opens_one(self, home):
        # 0.5.16: deploy_bundle is the only writer and there is no run engine
        # yet, so a per-service deploy must still be journal-visible.
        dj.record_event(home, "romm", dj.STATE_STARTING)
        record = dj.read_journal(home)
        assert record["run_id"]
        assert record["services"]["romm"]["state"] == "starting"

    def test_a_run_whose_rows_are_all_terminal_closes_itself(self, home):
        dj.record_event(home, "romm", dj.STATE_STARTING)
        assert dj.read_journal(home)["finished_at"] is None
        dj.record_event(home, "romm", dj.STATE_STARTED)
        assert dj.read_journal(home)["finished_at"] is not None

    def test_the_breaker_block_is_a_MIRROR_of_the_store(self, home):
        # design/60 §11.1 point 6: the journal renders breaker state, never owns
        # it — it is per-run and structurally cannot hold a cross-run count.
        context = breakers.deploy_context("romm")
        for _ in range(breakers.DEFAULT_THRESHOLD):
            breakers.record_failure(home, breakers.PLANE_DEPLOY, context, reason="boom")
        dj.record_event(home, "romm", dj.STATE_FAILED)
        mirror = dj.read_journal(home)["services"]["romm"]["breaker"]
        assert mirror["state"] == "open"
        assert mirror["consecutive_failures"] == breakers.DEFAULT_THRESHOLD
        # The count lives in the store, not in the journal.
        assert breakers.get_row(home, breakers.PLANE_DEPLOY, context)["consecutive_failures"] == 3

    def test_no_breaker_block_when_nothing_is_broken(self, home):
        dj.record_event(home, "romm", dj.STATE_STARTED)
        assert "breaker" not in dj.read_journal(home)["services"]["romm"]

    def test_summary_is_embeddable(self, home):
        dj.record_event(home, "romm", dj.STATE_STARTING)
        summary = dj.summary(home)
        assert summary["state"] == "in-flight"
        assert summary["in_flight"] == ["romm"]
        json.dumps(summary)  # must survive the --json contract
