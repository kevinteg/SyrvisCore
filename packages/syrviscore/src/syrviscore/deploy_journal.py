"""The deploy journal — what a deploy run was doing when it stopped.

``$SYRVIS_HOME/data/state/deploy-journal.json``, the contract RULED in
design/60 §5 **D6** (2026-08-16), which exists specifically to answer design/63
D3's ask — 63 M2 lists it as a shipping prerequisite (``opc:F3``)::

    {"schema_version": 1,
     "run_id": "20260816T114233Z-4711",
     "started_at": "2026-08-16T11:42:33Z",
     "finished_at": null,
     "by": "cli:deploy",
     "pid": 4711,
     "services": {"romm": {"state": "starting", "revision": 12, "at": "...",
                           "breaker": {...}}},
     "events": [{"at": "...", "service": "romm", "state": "starting", ...}]}

Four normative things, all from D6:

1. **Canonical path** — absolute, derived from ``SYRVIS_HOME``; never relative,
   never ``data/syrviscore/…``. It sits beside ``runstate.json``,
   ``intent.json``, ``breakers.json`` and (at M2) ``bringup.json``: one state
   directory, one convention.
2. **``schema_version`` is a required top-level integer.** A reader that does
   not recognise the version **reports ``unknown`` and refuses to act on the
   file** — it never guesses, and it never treats an unparseable journal as
   ABSENT. Absent means "no run"; unparseable means "I cannot tell", and those
   are different verdicts (the 2026-08-16 lesson about checks that report what
   they cannot see).
3. **Terminal-state set, enumerated** — see :data:`TERMINAL_STATES`. **``failed``
   IS terminal**: a failed service is a finished decision, not an in-flight one.
   A journal is *in-flight* iff any service row is non-terminal AND
   ``finished_at`` is absent.
4. **Staleness** — an in-flight journal older than :data:`STALE_AFTER_S`
   (60 minutes), or whose recorded pid is not live, is ``stale``. A stale
   journal **never blocks unattended work**: it is surfaced (design/61's gate F
   observes, never refuses) and it degrades design/63 D3's in-verb gate from
   refuse to annotate. Only a **fresh** in-flight journal whose in-flight
   service set *intersects* the bring-up set refuses, with ``--force`` parity.
   A stale journal that can hold unattended bring-up hostage forever is the
   failure mode this clause exists to forbid.

**On ``started`` vs ``healthy``** — D6 enumerated the terminal set before
design/60 §3.4's own amendment landed the ``started`` verdict, so the two read
together need one sentence: a step whose service declares **no healthcheck**
records ``started`` (container running), **never** ``healthy`` — and ``started``
is TERMINAL, because it is a completed step. Leaving it non-terminal would make
every deploy of a checkless service (15 of 39 on this fleet) look in-flight
forever, which is precisely the hostage-taking D6's staleness rule forbids.
At 0.5.16 there is no health gating at all (that is design/63 M2), so every
success records ``started`` — the honest state, since nothing was verified.

**Scope at 0.5.16.** This module ships the STORE and the deploy path's writes.
The consumers D6 enumerates — ``syrvis doctor``, ``syrvis up``'s gate,
``status --json``, hostd ``/status``, ``deploy-stack --resume``, the §9.4
docker-restart guardrail — read it from M2 onward; :func:`journal_status` is the
one function they all call, so the staleness rule is implemented exactly once.

Breaker blocks on service rows are **MIRRORS** rendered from
``data/state/breakers.json`` at write time (design/60 §11.1 point 6): the
journal is per-run and therefore structurally cannot own a cross-run count.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import paths
from .errors import SyrvisError

JOURNAL_SCHEMA_VERSION = 1

# Per-service states (design/60 §3.3 + §3.4 as amended).
STATE_PENDING = "pending"
STATE_STOPPING = "stopping"
STATE_STARTING = "starting"
STATE_STARTED = "started"
STATE_HEALTHY = "healthy"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

#: D6 clause 3, plus ``started`` (see the module docstring): a finished decision.
TERMINAL_STATES = frozenset({STATE_STARTED, STATE_HEALTHY, STATE_SKIPPED, STATE_FAILED})
NON_TERMINAL_STATES = frozenset({STATE_PENDING, STATE_STOPPING, STATE_STARTING})
STATES = TERMINAL_STATES | NON_TERMINAL_STATES

#: D6 clause 4 — 60 minutes.
STALE_AFTER_S = 3600

#: Bounded size (trim oldest). The journal is an operational record, not an
#: audit log — ``data/deployments/`` is the audit log, and it has its own cap.
MAX_EVENTS = 500

# journal_status() verdicts.
VERDICT_ABSENT = "absent"
VERDICT_UNKNOWN = "unknown"
VERDICT_IN_FLIGHT = "in-flight"
VERDICT_STALE = "stale"
VERDICT_TERMINAL = "terminal"


class JournalError(SyrvisError):
    """A deploy-journal record was rejected on the write path."""

    code = "deploy_journal_invalid"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_journal_path(home: Optional[Path] = None) -> Path:
    """D6 clause 1 — the canonical absolute path (absent == no run)."""
    return paths.get_state_dir(home) / "deploy-journal.json"


def new_run_id(pid: Optional[int] = None) -> str:
    """A run id that sorts chronologically and names the process that made it."""
    return "{}-{}".format(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), pid or os.getpid())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_journal(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The raw journal record, ``None`` when ABSENT.

    Raises nothing. An unparseable or wrong-version file is NOT None — it comes
    back as ``{"schema_version": <whatever>, "unreadable": True}`` so
    :func:`journal_status` can report ``unknown`` rather than "no run". Conflating
    those two is the exact failure D6 clause 2 forbids.
    """
    path = get_journal_path(home)
    try:
        if not path.exists():
            return None
    except OSError:
        return {"unreadable": True, "schema_version": None}
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - unparseable != absent
        return {"unreadable": True, "schema_version": None}
    if not isinstance(data, dict):
        return {"unreadable": True, "schema_version": None}
    if data.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        return {"unreadable": True, "schema_version": data.get("schema_version")}
    services = data.get("services")
    if not isinstance(services, dict):
        services = {}
    events = data.get("events")
    if not isinstance(events, list):
        events = []
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "run_id": data.get("run_id") or "",
        "started_at": data.get("started_at") or "",
        "finished_at": data.get("finished_at") or None,
        "by": data.get("by") or "",
        "pid": data.get("pid"),
        "services": {str(k): v for k, v in services.items() if isinstance(v, dict)},
        "events": [e for e in events if isinstance(e, dict)],
    }


def in_flight_services(record: Optional[Dict[str, Any]]) -> Set[str]:
    """Names whose row is NON-terminal (``pending``/``stopping``/``starting``)."""
    if not record or record.get("unreadable"):
        return set()
    return {
        name
        for name, row in (record.get("services") or {}).items()
        if row.get("state") in NON_TERMINAL_STATES
    }


def is_in_flight(record: Optional[Dict[str, Any]]) -> bool:
    """D6 clause 3's definition, in one place."""
    if not record or record.get("unreadable"):
        return False
    return bool(in_flight_services(record)) and not record.get("finished_at")


def _pid_alive(pid: Any) -> Optional[bool]:
    """True/False if we can tell, ``None`` if we cannot (no pid, other owner)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None


def is_stale(record: Optional[Dict[str, Any]], now: Optional[str] = None) -> bool:
    """D6 clause 4 — an in-flight journal that is old, or whose run is not live.

    Only an IN-FLIGHT journal can be stale; a finished run is simply history.
    """
    if not is_in_flight(record):
        return False
    if _pid_alive((record or {}).get("pid")) is False:
        return True
    started = _parse_ts((record or {}).get("started_at"))
    if started is None:
        return True  # an in-flight journal with no readable clock cannot be trusted fresh
    current = _parse_ts(now) or datetime.now(timezone.utc)
    return (current - started).total_seconds() > STALE_AFTER_S


def journal_status(home: Optional[Path] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """The ONE verdict function every D6 consumer calls.

    ``{"state": absent|unknown|in-flight|stale|terminal, "run_id", "started_at",
    "finished_at", "in_flight": [...], "act": bool, "detail"}``

    ``act`` is the answer to "may I act on this file?" — False for ``unknown``
    (D6 clause 2: report and refuse to act), True for everything else. A caller
    deciding whether to REFUSE a converge additionally checks whether its own
    service set intersects ``in_flight`` and whether the state is ``stale``:

    * ``stale``   -> annotate, never refuse (unattended work must not hang);
    * ``in-flight`` + intersecting -> refuse, with ``--force`` parity.
    """
    record = read_journal(home)
    if record is None:
        return {
            "state": VERDICT_ABSENT,
            "run_id": "",
            "started_at": "",
            "finished_at": None,
            "in_flight": [],
            "act": True,
            "detail": "no deploy run recorded",
        }
    if record.get("unreadable"):
        version = record.get("schema_version")
        return {
            "state": VERDICT_UNKNOWN,
            "run_id": "",
            "started_at": "",
            "finished_at": None,
            "in_flight": [],
            "act": False,
            "detail": (
                "deploy journal declares schema_version {!r}, this reader knows {} — "
                "refusing to act on a file it cannot read".format(version, JOURNAL_SCHEMA_VERSION)
                if version is not None
                else "deploy journal is unparseable — refusing to act on it "
                "(unparseable is not 'no run')"
            ),
        }
    pending = sorted(in_flight_services(record))
    if not is_in_flight(record):
        return {
            "state": VERDICT_TERMINAL,
            "run_id": record["run_id"],
            "started_at": record["started_at"],
            "finished_at": record["finished_at"],
            "in_flight": [],
            "act": True,
            "detail": "last run {} finished".format(record["run_id"] or "(unnamed)"),
        }
    stale = is_stale(record, now=now)
    return {
        "state": VERDICT_STALE if stale else VERDICT_IN_FLIGHT,
        "run_id": record["run_id"],
        "started_at": record["started_at"],
        "finished_at": None,
        "in_flight": pending,
        "act": True,
        "detail": (
            "run {} left {} service(s) in flight and is older than {}s (or its "
            "process is gone) — annotate, never refuse".format(
                record["run_id"] or "(unnamed)", len(pending), STALE_AFTER_S
            )
            if stale
            else "run {} has {} service(s) in flight".format(
                record["run_id"] or "(unnamed)", len(pending)
            )
        ),
    }


def intersects(home: Optional[Path], services: Any, now: Optional[str] = None) -> List[str]:
    """The FRESH in-flight services that overlap ``services`` (D6 clause 4).

    Empty for an absent, terminal, unknown or STALE journal — a stale journal
    never refuses anything. This is the whole predicate design/63 D3's in-verb
    gate needs, so no caller re-derives it.
    """
    status = journal_status(home, now=now)
    if status["state"] != VERDICT_IN_FLIGHT:
        return []
    return sorted(set(status["in_flight"]) & set(services or ()))


# ---------------------------------------------------------------------------
# Write (best-effort: a journal failure must never fail a deployment)
# ---------------------------------------------------------------------------


def _write(home: Optional[Path], record: Dict[str, Any]) -> Dict[str, Any]:
    """Persist atomically (temp + rename, 0644), trimming events to the cap."""
    events = record.get("events") or []
    if len(events) > MAX_EVENTS:
        record["events"] = events[-MAX_EVENTS:]  # oldest out
    record["schema_version"] = JOURNAL_SCHEMA_VERSION
    path = get_journal_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".{}.tmp".format(os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, json.dumps(record, indent=2, default=str).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, str(path))
    try:
        Path(path).chmod(0o644)
    except OSError:
        pass
    return record


def begin_run(
    home: Optional[Path],
    services: Any = (),
    by: str = "",
    run_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a run: every named service starts ``pending``. Replaces any prior run.

    Deliberately a REPLACE, not an append: the journal answers "what is
    happening now", and D6 gives it exactly one ``run_id``. Run HISTORY lives in
    ``data/deployments/`` (per-workload, immutable, capped), which is the record
    that must never be lost.
    """
    stamp = now or _utc_now()
    record = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "run_id": run_id or new_run_id(),
        "started_at": stamp,
        "finished_at": None,
        "by": by,
        "pid": os.getpid(),
        "services": {
            str(name): {"state": STATE_PENDING, "revision": None, "at": stamp}
            for name in (services or ())
        },
        "events": [],
    }
    return _write(home, record)


def record_event(
    home: Optional[Path],
    service: str,
    state: str,
    revision: Optional[int] = None,
    detail: str = "",
    by: str = "",
    now: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Append one event and update the service's row. Never raises.

    Written *before* each state transition (design/60 §3.3), so a killed run
    leaves the truth on disk rather than an optimistic guess. If no run is open
    — the 0.5.16 case, where ``deploy_bundle`` is the only writer and there is no
    run engine yet — one is opened implicitly for this single service, which is
    what makes a per-service deploy journal-visible without inventing design/63
    M2's engine early.

    The service row's ``breaker`` block is MIRRORED from ``breakers.json``
    (design/60 §11.1 point 6) — never owned here.
    """
    try:
        if state not in STATES:
            raise JournalError(
                "unknown journal state {!r}: must be one of {}".format(
                    state, ", ".join(sorted(STATES))
                )
            )
        stamp = now or _utc_now()
        record = read_journal(home)
        if record is None or record.get("unreadable") or record.get("finished_at"):
            # No open run (or one we must not touch): open one for this service.
            record = begin_run(home, services=[service], by=by, now=stamp)
        row = dict(record["services"].get(service) or {})
        row["state"] = state
        row["at"] = stamp
        if revision is not None:
            row["revision"] = revision
        if detail:
            row["detail"] = detail
        elif "detail" in row:
            row.pop("detail")
        mirror = _breaker_mirror(home, service)
        if mirror is not None:
            row["breaker"] = mirror
        else:
            row.pop("breaker", None)
        record["services"][service] = row
        if by:
            record["by"] = by
        event = {"at": stamp, "service": service, "state": state}
        if revision is not None:
            event["revision"] = revision
        if detail:
            event["detail"] = detail
        record["events"] = list(record.get("events") or []) + [event]
        # A run whose every row is terminal is finished — otherwise a per-service
        # deploy would leave a permanently "in-flight" journal that goes stale
        # after an hour and annotates every later bring-up for no reason.
        if record["services"] and not in_flight_services(record):
            record["finished_at"] = stamp
        return _write(home, record)
    except Exception:  # noqa: BLE001 - journalling must never fail a deployment
        return None


def _breaker_mirror(home: Optional[Path], service: str) -> Optional[Dict[str, Any]]:
    try:
        from . import breakers

        return breakers.mirror_for(home, breakers.PLANE_DEPLOY, breakers.deploy_context(service))
    except Exception:  # noqa: BLE001 - a mirror is never load-bearing
        return None


def finish_run(home: Optional[Path], now: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Close the open run. Never raises; a no-op when nothing is open."""
    try:
        record = read_journal(home)
        if record is None or record.get("unreadable") or record.get("finished_at"):
            return None
        record["finished_at"] = now or _utc_now()
        return _write(home, record)
    except Exception:  # noqa: BLE001
        return None


def summary(home: Optional[Path] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """The block ``status --json`` / ``syrvis doctor`` / hostd ``/status`` render."""
    status = journal_status(home, now=now)
    record = read_journal(home)
    services = {} if (not record or record.get("unreadable")) else record.get("services") or {}
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "state": status["state"],
        "run_id": status["run_id"],
        "started_at": status["started_at"],
        "finished_at": status["finished_at"],
        "in_flight": status["in_flight"],
        "act": status["act"],
        "detail": status["detail"],
        "services": services,
    }
