"""Circuit breakers — ONE durable store, one union view, one reset path.

``$SYRVIS_HOME/data/state/breakers.json`` is the **only** place a breaker count
lives (design/60 §11.1 point 6, added 2026-08-16 for ``opc:F1``/``opc:F4``).
Every other surface that shows a breaker — the deploy journal's per-service
``breaker:`` block, design/63's ``bringup.json`` ``breakers[]``, hostd's
``/status.breakers``, ``syrvis doctor``'s table, the ops-console panel — is a
**MIRROR rendered from this file**, never an owner::

    {"schema_version": 1,
     "breakers": [{"plane": "deploy", "context": "deploy-svc:romm",
                   "state": "open", "consecutive_failures": 3,
                   "opened_at": "...", "last_probe_at": null,
                   "next_probe_at": "...", "reason": "...", "by": "cli:deploy"}]}

**Why a store and not per-surface state.** The journal is per-RUN and
``bringup.json`` is overwritten by the next engine run, so neither can hold a
count "across runs" — which is exactly what a breaker is. Worse, a per-plane
store gave one sick service two independent open breakers, each counting the
same failures, each half-open probing, each paging once for the same fact, and
each cleared by a *different* verb with neither admitting the other existed.

The four doctrine rules this module implements (design/60 §11.1):

* **The curve** — exponential with jitter, per-context base, **capped at 10
  minutes**. Half-open probes land at the cap.
* **Named states** — ``closed`` / ``open`` / ``half-open``, carried as DATA.
  A reader never infers a breaker from a count.
* **Cross-plane suppression** — an open breaker in ANY plane suppresses
  *automatic* work in EVERY plane for that service. Automatic ≠ operator: an
  explicit operator verb always proceeds.
* **A close closes all** — any in-scope operator-intent verb closes every
  plane's breaker for that service and zeroes every count, so
  ``deploy-stack --only X`` and ``syrvis up`` stop being half-measures that each
  leave the other's breaker armed.

**``by`` is a FIELD, not a doctrine** (``opc:F2``): "operator intent" cannot be
inferred from the verb, because hostd, the S99 fallback and ``restore`` all fire
the same ``syrvis up``. Only ``by ∈ {cli:*, seam:*, mcp:*}`` closes breakers;
``{hostd, s99, cron}`` INHERITS and honors them, and an absent/unknown value
fails safe toward *not* resetting.

Scope at 0.5.16: this module ships the STORE and its arithmetic. The consuming
engine — the recovery loop, half-open probing on a timer, the once-per-open page
— is design/63 M2. Writes are atomic (temp + rename) and 0644, matching
``intent.json``/``runstate.json``: the rows carry service names and reasons,
never a secret, and the unprivileged seam reader must be able to answer "is this
deliberate?" without sudo.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import paths
from .errors import SyrvisError

BREAKERS_SCHEMA_VERSION = 1

#: design/60 §11.1 point 6 — the enumerated planes.
PLANE_DEPLOY = "deploy"
PLANE_RECOVERY = "recovery"
PLANE_AGENT = "agent"
PLANES = (PLANE_DEPLOY, PLANE_RECOVERY, PLANE_AGENT)

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half-open"
STATES = (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN)

#: Consecutive failures that open a breaker (design/60 §11.2, design/63 §6.2).
DEFAULT_THRESHOLD = 3
#: The doctrine curve: base doubles with jitter, hard-capped at ten minutes.
DEFAULT_BASE_S = 30
MAX_BACKOFF_S = 600
#: ±20% jitter, so a fleet of breakers never re-probes in lockstep.
JITTER_FRACTION = 0.2

#: Bound the file. A row per {plane, context}; a fleet of 39 services across
#: three planes cannot legitimately exceed this, and an unbounded state file on
#: a NAS is how a disk fills at 3 a.m.
MAX_ROWS = 512

#: ``by`` prefixes that represent OPERATOR INTENT and therefore CLOSE breakers.
#: Everything else — hostd, the S99 fallback, cron — inherits and honors them.
OPERATOR_BY_PREFIXES = ("cli:", "seam:", "mcp:")
INHERITING_BY = ("hostd", "s99", "cron")


class BreakerError(SyrvisError):
    """A breaker row or mutation was rejected (unknown plane/state)."""

    code = "breaker_invalid"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse one of our own ``...Z`` stamps; ``None`` on anything unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Contexts — the plane's own key for the thing being broken
# ---------------------------------------------------------------------------


def deploy_context(name: str) -> str:
    """The deploy plane's context for a service (design/60 §11.2)."""
    return "deploy-svc:{}".format(name)


def node_context(name: str) -> str:
    """The recovery plane's context for a service (design/63 §6.2)."""
    return "node:{}".format(name)


def service_contexts(name: str) -> List[Tuple[str, str]]:
    """Every ``(plane, context)`` pair that can hold a breaker for ONE service.

    This tuple is what makes "a close closes all" implementable: the two planes
    key the same service differently, so the union is a concatenation over this
    list rather than a join anyone could get wrong.
    """
    return [(PLANE_DEPLOY, deploy_context(name)), (PLANE_RECOVERY, node_context(name))]


def closes_breakers(by: Optional[str]) -> bool:
    """True iff ``by`` represents operator intent (design/60 §11.1 point 6).

    Fails SAFE: an absent or unrecognised value is treated as the inheriting
    class, i.e. it does NOT reset. A caller that cannot prove operator intent
    must not be able to assert it — which is why the seam registry supplies the
    value rather than the caller.
    """
    if not by or not isinstance(by, str):
        return False
    return by.startswith(OPERATOR_BY_PREFIXES)


# ---------------------------------------------------------------------------
# The curve
# ---------------------------------------------------------------------------


def backoff_seconds(
    consecutive_failures: int, base_s: int = DEFAULT_BASE_S, jitter: bool = True
) -> int:
    """Exponential backoff with jitter, capped at :data:`MAX_BACKOFF_S`.

    ``failures=1`` -> ~base, then doubling, never past ten minutes. The cap is
    doctrine (design/60 §11), not a tuning knob: a half-open probe every ten
    minutes is the slowest anything in this platform is allowed to re-try.
    """
    if consecutive_failures < 1:
        return 0
    # Bound the exponent before shifting: 2**(large int) is a memory bomb.
    steps = min(int(consecutive_failures) - 1, 20)
    delay = min(int(base_s) * (2**steps), MAX_BACKOFF_S)
    if jitter:
        spread = delay * JITTER_FRACTION
        delay = int(delay + random.uniform(-spread, spread))
    return max(1, min(delay, MAX_BACKOFF_S))


# ---------------------------------------------------------------------------
# Store I/O
# ---------------------------------------------------------------------------


def get_breakers_path(home: Optional[Path] = None) -> Path:
    """Path to the durable breaker store (absent == every breaker closed)."""
    return paths.get_state_dir(home) / "breakers.json"


def empty_store() -> Dict[str, Any]:
    return {"schema_version": BREAKERS_SCHEMA_VERSION, "breakers": []}


def _normalize_row(raw: Any) -> Optional[Dict[str, Any]]:
    """One validated row, or ``None`` to drop it (per-row isolation)."""
    if not isinstance(raw, dict):
        return None
    plane, context = raw.get("plane"), raw.get("context")
    if plane not in PLANES or not isinstance(context, str) or not context:
        return None
    state = raw.get("state")
    if state not in STATES:
        state = STATE_CLOSED
    try:
        failures = int(raw.get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    return {
        "plane": plane,
        "context": context,
        "state": state,
        "consecutive_failures": max(0, failures),
        "opened_at": raw.get("opened_at") or None,
        "last_probe_at": raw.get("last_probe_at") or None,
        "next_probe_at": raw.get("next_probe_at") or None,
        "reason": raw.get("reason") or "",
        "by": raw.get("by") or "",
    }


def read_breakers(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every breaker row — NEVER raises; absent/unreadable/garbage == ``[]``.

    A read must never be the thing that breaks a deploy, a status call or a
    dashboard poll, and "no file" is the overwhelmingly common case. Rows that
    fail validation are dropped INDIVIDUALLY, the same per-row isolation
    ``load_declarations`` gives declarations and ``read_intent`` gives shed rows.
    """
    try:
        path = get_breakers_path(home)
        if not path.exists():
            return []
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - unreadable store degrades to "all closed"
        return []
    if not isinstance(data, dict):
        return []
    if data.get("schema_version") != BREAKERS_SCHEMA_VERSION:
        # Unknown version: report nothing rather than guess at the shape. A
        # WRITE would clobber a newer reader's file, so callers get an empty
        # view and the file is left alone (see write_breakers' refusal).
        return []
    rows = []
    seen = set()
    for raw in data.get("breakers") or []:
        row = _normalize_row(raw)
        if row is None:
            continue
        key = (row["plane"], row["context"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["plane"], r["context"]))


def write_breakers(home: Optional[Path], rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Persist the row set atomically (temp + rename, 0644). Validates first.

    Refuses to overwrite a store whose ``schema_version`` this reader does not
    recognise — a newer platform's state file must survive an older binary
    touching the box (the rollback door design/63 D2 spends a paragraph on).
    """
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for raw in rows:
        row = _normalize_row(raw)
        if row is None:
            raise BreakerError("invalid breaker row: {!r}".format(raw))
        key = (row["plane"], row["context"])
        if key in seen:
            raise BreakerError("duplicate breaker row for {}".format(key))
        seen.add(key)
        normalized.append(row)
    if len(normalized) > MAX_ROWS:
        # Drop CLOSED rows first — a closed breaker is the absence of news.
        normalized.sort(key=lambda r: (r["state"] == STATE_CLOSED, r["plane"], r["context"]))
        normalized = normalized[:MAX_ROWS]
    normalized.sort(key=lambda r: (r["plane"], r["context"]))

    path = get_breakers_path(home)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            version = existing.get("schema_version") if isinstance(existing, dict) else None
            if version is not None and version != BREAKERS_SCHEMA_VERSION:
                raise BreakerError(
                    "breakers.json declares schema_version {!r}, this platform writes {} — "
                    "refusing to overwrite state written by a different "
                    "version".format(version, BREAKERS_SCHEMA_VERSION)
                )
        except BreakerError:
            raise
        except Exception:  # noqa: BLE001 - unparseable: we own it, rewrite it
            pass

    record = {"schema_version": BREAKERS_SCHEMA_VERSION, "breakers": normalized}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".{}.tmp".format(os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, json.dumps(record, indent=2).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, str(path))
    try:
        Path(path).chmod(0o644)
    except OSError:
        pass
    return normalized


# ---------------------------------------------------------------------------
# Row access
# ---------------------------------------------------------------------------


def get_row(home: Optional[Path], plane: str, context: str) -> Optional[Dict[str, Any]]:
    """The row for one ``{plane, context}``, or ``None`` when never recorded."""
    for row in read_breakers(home):
        if row["plane"] == plane and row["context"] == context:
            return row
    return None


def _put(home: Optional[Path], row: Dict[str, Any]) -> Dict[str, Any]:
    rows = [
        r
        for r in read_breakers(home)
        if not (r["plane"] == row["plane"] and r["context"] == row["context"])
    ]
    rows.append(row)
    write_breakers(home, rows)
    return row


def record_failure(
    home: Optional[Path],
    plane: str,
    context: str,
    reason: str = "",
    by: str = "",
    threshold: int = DEFAULT_THRESHOLD,
    base_s: int = DEFAULT_BASE_S,
    now: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Count one failure. Returns ``(row, opened_transition)``.

    ``opened_transition`` is True on the **transition** into ``open`` and only
    then — that boolean is the whole of design/60 §11.1 point 4 ("the open
    transition pages exactly once"). A later failure against an already-open
    breaker re-arms the curve and returns False, so a caller that pages on True
    can never double-page; persistence belongs to the durable plane (the staged
    metric rule, the red dead-man).
    """
    if plane not in PLANES:
        raise BreakerError("unknown plane {!r}: must be one of {}".format(plane, ", ".join(PLANES)))
    stamp = now or _utc_now()
    row = get_row(home, plane, context) or {
        "plane": plane,
        "context": context,
        "state": STATE_CLOSED,
        "consecutive_failures": 0,
        "opened_at": None,
        "last_probe_at": None,
        "next_probe_at": None,
        "reason": "",
        "by": "",
    }
    was_open = row["state"] == STATE_OPEN
    row["consecutive_failures"] = int(row["consecutive_failures"]) + 1
    row["reason"] = reason or row.get("reason") or ""
    row["by"] = by or row.get("by") or ""
    row["last_probe_at"] = stamp
    opened = False
    if row["consecutive_failures"] >= max(1, int(threshold)):
        row["state"] = STATE_OPEN
        if not was_open:
            row["opened_at"] = stamp
            opened = True
        delay = backoff_seconds(row["consecutive_failures"], base_s=base_s)
        row["next_probe_at"] = _stamp_plus(stamp, delay)
    else:
        # Below the threshold the breaker is still CLOSED — but the curve is
        # already running, so the next automatic attempt waits.
        row["state"] = STATE_CLOSED
        row["next_probe_at"] = _stamp_plus(
            stamp, backoff_seconds(row["consecutive_failures"], base_s=base_s)
        )
    return _put(home, row), opened


def record_success(
    home: Optional[Path],
    plane: str,
    context: str,
    by: str = "",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """A successful attempt CLOSES the breaker and zeroes the count.

    Doctrine point 2: "one probe success closes it and resets the curve to
    base". Unlike :func:`close_service` this is the plane's own observation, not
    an operator reset, so it touches exactly this row.
    """
    stamp = now or _utc_now()
    return _put(
        home,
        {
            "plane": plane,
            "context": context,
            "state": STATE_CLOSED,
            "consecutive_failures": 0,
            "opened_at": None,
            "last_probe_at": stamp,
            "next_probe_at": None,
            "reason": "",
            "by": by,
        },
    )


def _stamp_plus(stamp: str, seconds: int) -> str:
    base = _parse_ts(stamp) or datetime.now(timezone.utc)
    return (base + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def should_attempt(
    home: Optional[Path],
    plane: str,
    context: str,
    now: Optional[str] = None,
) -> Tuple[bool, bool]:
    """``(attempt, is_half_open_probe)`` for an AUTOMATIC attempt.

    design/60 §11.2 as AMENDED 2026-08-16 (``opc:F5``) — half-open ruled once,
    because the paragraph previously gave two opposite answers for the same
    event and "one attempt per interval" had no actor (there is no loop driving
    deploys; a deploy happens when a human runs one). The single sentence:

        an attempt issued BEFORE ``next_probe_at`` skips; the FIRST attempt
        issued AFTER ``next_probe_at`` IS the half-open probe.

    A half-open probe's FAILURE does not page — it re-opens silently and re-arms
    the curve; its success closes the breaker. Callers pass the result of this
    call straight into :func:`record_failure` / :func:`record_success`.

    An OPERATOR verb does not consult this at all: automatic ≠ operator.
    """
    row = get_row(home, plane, context)
    if row is None or row["state"] == STATE_CLOSED and not row.get("next_probe_at"):
        return True, False
    deadline = _parse_ts(row.get("next_probe_at"))
    if deadline is None:
        return True, False
    current = _parse_ts(now) or datetime.now(timezone.utc)
    if current < deadline:
        return False, False
    return True, row["state"] == STATE_OPEN


def suppressed_by(home: Optional[Path], name: str) -> Optional[Dict[str, Any]]:
    """The open row (ANY plane) that suppresses automatic work for ``name``.

    Cross-plane suppression, design/60 §11.1 point 6: *an open breaker in ANY
    plane suppresses automatic work in EVERY plane for that service.* The deploy
    run marks it ``skipped (breaker open, plane=recovery)``; the recovery loop
    declines to re-attempt a service whose deploy breaker is open. Returns the
    first open row so the caller can name the plane in its message.
    """
    rows = {(r["plane"], r["context"]): r for r in read_breakers(home)}
    for plane, context in service_contexts(name):
        row = rows.get((plane, context))
        if row is not None and row["state"] in (STATE_OPEN, STATE_HALF_OPEN):
            return row
    return None


def close_service(
    home: Optional[Path], name: str, by: str = "cli", now: Optional[str] = None
) -> List[Dict[str, Any]]:
    """**A close closes all** — every plane's breaker for ONE service.

    design/60 §11.1 point 6's reset path. Returns the rows that were actually
    cleared (empty when the service had none), so a caller can report the reset
    it just performed. Deliberately unconditional on ``by``: the CALLER decides
    whether it holds operator intent (:func:`closes_breakers` is the test), and
    only then calls this.
    """
    stamp = now or _utc_now()
    rows = read_breakers(home)
    keys = set(service_contexts(name))
    cleared = [r for r in rows if (r["plane"], r["context"]) in keys and _is_live(r)]
    if not cleared:
        return []
    kept = [r for r in rows if (r["plane"], r["context"]) not in keys]
    for plane, context in sorted(keys):
        kept.append(
            {
                "plane": plane,
                "context": context,
                "state": STATE_CLOSED,
                "consecutive_failures": 0,
                "opened_at": None,
                "last_probe_at": stamp,
                "next_probe_at": None,
                "reason": "",
                "by": by,
            }
        )
    write_breakers(home, kept)
    return cleared


def _is_live(row: Dict[str, Any]) -> bool:
    """True iff the row carries state worth clearing (not already at rest)."""
    return row["state"] != STATE_CLOSED or int(row.get("consecutive_failures") or 0) > 0


def close_scope(
    home: Optional[Path], services: Iterable[str], by: str = "cli", now: Optional[str] = None
) -> List[Dict[str, Any]]:
    """:func:`close_service` over a set — the shape an operator verb's scope has.

    Best-effort by construction: a breaker reset must never be the thing that
    fails the verb the operator already authorized.
    """
    cleared: List[Dict[str, Any]] = []
    for name in sorted(set(services or ())):
        try:
            cleared.extend(close_service(home, name, by=by, now=now))
        except Exception:  # noqa: BLE001 - a reset never blocks the verb
            continue
    return cleared


def mirror_for(home: Optional[Path], plane: str, context: str) -> Optional[Dict[str, Any]]:
    """The small ``breaker:`` block a MIRROR surface embeds, or ``None``.

    design/60 §11.2's "visibility recap": journal per-service rows carry
    ``{state, consecutive_failures, opened_at, next_probe_at}`` **mirrored from
    breakers.json**. Returning ``None`` for a never-recorded context keeps the
    mirror absent rather than inventing a closed row on every write.
    """
    row = get_row(home, plane, context)
    if row is None or not _is_live(row):
        return None
    return {
        "state": row["state"],
        "consecutive_failures": row["consecutive_failures"],
        "opened_at": row["opened_at"],
        "next_probe_at": row["next_probe_at"],
    }


def summary(home: Optional[Path] = None) -> Dict[str, Any]:
    """The block ``status --json`` / ``syrvis doctor`` / the console panel render.

    The UNION view — every plane, one concatenation (each row already carries
    its own ``plane``, so no join is possible to get wrong).
    """
    rows = read_breakers(home)
    open_rows = [r for r in rows if r["state"] in (STATE_OPEN, STATE_HALF_OPEN)]
    by_plane: Dict[str, int] = {}
    for row in open_rows:
        by_plane[row["plane"]] = by_plane.get(row["plane"], 0) + 1
    return {
        "schema_version": BREAKERS_SCHEMA_VERSION,
        "breakers": rows,
        "open": [r["context"] for r in open_rows],
        "open_count": len(open_rows),
        "open_by_plane": by_plane,
    }
