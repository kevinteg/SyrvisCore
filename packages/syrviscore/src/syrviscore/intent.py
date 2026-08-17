"""Declared operational intent — the durable answer to "this is deliberate".

One small root-owned JSON at ``$SYRVIS_HOME/data/state/intent.json`` records
what an operator has DECIDED about this instance, as opposed to what its
declarations describe or what Docker currently reports:

    {"schema_version": 1,
     "device": "in-service",
     "shed": [{"service": "onyx-api", "reason": "md6-resync",
               "since": "2026-08-16T12:00:00Z", "until": "2026-08-24"}]}

**Why it is not an ``enabled:`` flag.** ``services.d`` declarations are GitOps
state: a deployment repo renders them and ``syrvis apply`` writes the whole set
as a replace set. So the flag that says "this service is deliberately down"
lived in exactly the file the next apply overwrites — which is how fourteen
load-shed services could be resurrected, mid-array-resync, by a runbook whose
own text said they would stay down (incident 2026-08-16). Intent lives OUTSIDE
the declaration set by construction: nothing in the bundle path can address it,
so it survives apply, deploy, reconcile, resume and boot.

**The composition rule** is one sentence, and every consumer implements exactly
it: *a workload runs iff the device is ``in-service`` AND the service is
``enabled`` AND the service is not shed.* Shed is therefore an
``enabled: false`` OVERLAY — the reconcile planner treats a shed service the
way it treats a declared-off one (never start it, stop it if it is somehow
alive, never call it drift), while the declaration underneath keeps saying what
the service is FOR, so lifting the shed restores it with no re-declaration.

**Absent file == empty intent.** Every read degrades to
``{"device": "in-service", "shed": []}`` so an instance that has never shed
anything — and every older reader — behaves exactly as before. Writes are
atomic (temp + rename) and 0644: the state is names and reasons, never secrets,
and the unprivileged reader identity must be able to see it.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import paths
from .errors import SyrvisError

INTENT_SCHEMA_VERSION = 1
INTENT_SCHEMA = "syrvis-intent/v1"

DEVICE_IN_SERVICE = "in-service"
DEVICE_DRAINED = "drained"
DEVICE_INTENTS = (DEVICE_IN_SERVICE, DEVICE_DRAINED)

# A shed reason is a short machine-readable TOKEN (``md6-resync``,
# ``vendor-outage``), not prose: it is carried on argv over the operator seam,
# whose forced-command shim word-splits and character-allowlists everything it
# receives, and it becomes a Prometheus label value. Prose belongs in the
# runbook the reason points at.
SHED_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# ``until`` is a date or a UTC timestamp — the two shapes an operator actually
# writes, both seam-safe (':' and '-' are on the shim's charset allowlist).
SHED_UNTIL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z)?$")


class IntentError(SyrvisError):
    """An intent record or mutation was rejected (malformed or unknown value)."""

    code = "intent_invalid"


class ServiceShedError(SyrvisError):
    """A bring-up was refused because the service is deliberately shed."""

    code = "service_shed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_intent_path(home: Optional[Path] = None) -> Path:
    """Path to the intent record (absent == empty intent)."""
    return paths.get_state_dir(home) / "intent.json"


def empty_intent() -> Dict[str, Any]:
    """The intent every instance has before anything is declared about it."""
    return {"schema_version": INTENT_SCHEMA_VERSION, "device": DEVICE_IN_SERVICE, "shed": []}


def read_intent(home: Optional[Path] = None) -> Dict[str, Any]:
    """The current intent — NEVER raises, absent/unreadable/garbage == empty.

    A read must never be the thing that breaks a reconcile, a status call or a
    dashboard poll, and "no file" is the overwhelmingly common case. Rows that
    fail validation are dropped INDIVIDUALLY (the same per-row isolation
    ``load_declarations`` gives declarations) so one bad row cannot erase the
    other thirteen.
    """
    try:
        path = get_intent_path(home)
        if not path.exists():
            return empty_intent()
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - unreadable intent degrades to empty
        return empty_intent()
    if not isinstance(data, dict):
        return empty_intent()

    device = data.get("device")
    if device not in DEVICE_INTENTS:
        device = DEVICE_IN_SERVICE
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in data.get("shed") or []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("service")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "service": name,
                "reason": raw.get("reason") or "unspecified",
                "since": raw.get("since") or "",
                "until": raw.get("until") or None,
                "by": raw.get("by") or "",
            }
        )
    schema = data.get("schema_version")
    return {
        "schema_version": schema if isinstance(schema, int) else INTENT_SCHEMA_VERSION,
        "device": device,
        "shed": sorted(rows, key=lambda r: r["service"]),
    }


def validate_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an intent document on the WRITE path (raises :class:`IntentError`).

    Reads are forgiving; writes are not. A malformed record that reached disk
    would degrade silently on every later read (see :func:`read_intent`), and a
    silently-empty shed list is exactly the failure this module exists to end.
    """
    if not isinstance(intent, dict):
        raise IntentError("intent must be a mapping")
    device = intent.get("device", DEVICE_IN_SERVICE)
    if device not in DEVICE_INTENTS:
        raise IntentError(
            "device intent must be one of {} (got {!r})".format(", ".join(DEVICE_INTENTS), device)
        )
    rows = intent.get("shed") or []
    if not isinstance(rows, list):
        raise IntentError("'shed' must be a list of rows")
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise IntentError("each shed row must be a mapping")
        name = raw.get("service")
        if not isinstance(name, str) or not name:
            raise IntentError("shed row is missing 'service'")
        if name in seen:
            raise IntentError("duplicate shed row for {!r}".format(name))
        seen.add(name)
        out.append(
            {
                "service": name,
                "reason": validate_reason(raw.get("reason") or ""),
                "since": raw.get("since") or _utc_now(),
                "until": validate_until(raw.get("until")),
                "by": raw.get("by") or "",
            }
        )
    return {
        "schema_version": INTENT_SCHEMA_VERSION,
        "device": device,
        "shed": sorted(out, key=lambda r: r["service"]),
    }


def validate_reason(reason: str) -> str:
    if not isinstance(reason, str) or not SHED_REASON_RE.match(reason):
        raise IntentError(
            "shed reason must match {} (a short token like 'md6-resync', got {!r})".format(
                SHED_REASON_RE.pattern, reason
            )
        )
    return reason


def validate_until(until: Optional[str]) -> Optional[str]:
    if until in (None, ""):
        return None
    if not isinstance(until, str) or not SHED_UNTIL_RE.match(until):
        raise IntentError(
            "shed 'until' must be YYYY-MM-DD or YYYY-MM-DDThh:mm[:ss]Z (got {!r})".format(until)
        )
    return until


def write_intent(home: Optional[Path], intent: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the intent atomically (temp + rename, 0644). Validates first.

    0644 matches ``runstate.json``: the record carries service names, reasons
    and timestamps — never a secret — and the unprivileged seam reader has to
    be able to answer "is this deliberate?" without sudo.
    """
    record = validate_intent(intent)
    path = get_intent_path(home)
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
    return record


# ---------------------------------------------------------------------------
# Shed set — the per-service half
# ---------------------------------------------------------------------------


def shed_rows(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    return read_intent(home)["shed"]


def shed_map(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """``{service: row}`` for the shed set (the form every consumer wants)."""
    return {row["service"]: row for row in shed_rows(home)}


def shed_names(home: Optional[Path] = None) -> Set[str]:
    return {row["service"] for row in shed_rows(home)}


def is_shed(home: Optional[Path], name: str) -> bool:
    return name in shed_names(home)


def device_intent(home: Optional[Path] = None) -> str:
    return read_intent(home)["device"]


def shed(
    home: Optional[Path],
    name: str,
    reason: str,
    until: Optional[str] = None,
    by: str = "cli",
) -> Dict[str, Any]:
    """Declare ``name`` deliberately down. Idempotent (re-shedding re-states it).

    Returns the row written. The CALLER stops the container — this function
    only owns the durable record, so a stop failure can never leave the intent
    unwritten (and a re-shed of an already-stopped service is free).
    """
    row = {
        "service": name,
        "reason": validate_reason(reason),
        "since": _utc_now(),
        "until": validate_until(until),
        "by": by,
    }
    intent = read_intent(home)
    rows = [r for r in intent["shed"] if r["service"] != name]
    # Re-shedding keeps the ORIGINAL since — "how long has this been down" is
    # the question the field exists to answer, and a repeated shed (a second
    # apply, a re-run of the same runbook step) must not reset the clock.
    previous = {r["service"]: r for r in intent["shed"]}.get(name)
    if previous and previous.get("since"):
        row["since"] = previous["since"]
    rows.append(row)
    intent["shed"] = rows
    write_intent(home, intent)
    return row


def unshed(home: Optional[Path], name: str) -> Optional[Dict[str, Any]]:
    """Lift the shed on ``name``. Returns the removed row, or None if not shed.

    Lifting does NOT start anything: the declaration underneath was never
    touched, so the next ``reconcile`` (or an explicit ``service start``) is
    what brings the service back — one bring-up path, not two.
    """
    intent = read_intent(home)
    removed = None
    rows = []
    for row in intent["shed"]:
        if row["service"] == name:
            removed = row
        else:
            rows.append(row)
    if removed is None:
        return None
    intent["shed"] = rows
    write_intent(home, intent)
    return removed


def expired(home: Optional[Path] = None, now: Optional[str] = None) -> List[Dict[str, Any]]:
    """Shed rows whose ``until`` has passed — a shed nobody lifted.

    String comparison is correct here and deliberate: both shapes accepted by
    :data:`SHED_UNTIL_RE` are zero-padded and lexicographically ordered, and a
    date-only ``until`` compares less than any timestamp on that date, so it
    expires at midnight UTC. Rows with no ``until`` never expire (manual lift).
    """
    stamp = now or _utc_now()
    return [r for r in shed_rows(home) if r.get("until") and str(r["until"]) < stamp]


def summary(home: Optional[Path] = None) -> Dict[str, Any]:
    """The block ``status --json`` and the dashboard render.

    Counts and names only — small enough to embed anywhere, complete enough
    that a consumer never has to open the file itself.
    """
    intent = read_intent(home)
    rows = intent["shed"]
    reasons: Dict[str, int] = {}
    for row in rows:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    return {
        "schema_version": intent["schema_version"],
        "device": intent["device"],
        "drained": intent["device"] == DEVICE_DRAINED,
        "shed": rows,
        "shed_count": len(rows),
        "shed_reasons": reasons,
        "shed_expired": [r["service"] for r in expired(home)],
    }


# ---------------------------------------------------------------------------
# Override journal
# ---------------------------------------------------------------------------


def log_override(
    home: Optional[Path],
    action: str,
    flag: str,
    detail: Dict[str, Any],
) -> None:
    """Append one structured line to ``logs/overrides.log`` (best-effort).

    Every guard in this family is overridable on purpose — the operator has
    reasons the platform cannot see. What must never happen is an override
    that leaves no trace, so the refusal and the bypass cost the same one line
    of evidence. Best-effort, exactly like the hook log: a read-only logs/
    must not be able to block the verb the operator already authorized.
    """
    try:
        base = Path(home) if home is not None else paths.get_syrvis_home()
        logs = base / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"at": _utc_now(), "action": action, "override": flag, "detail": detail},
            default=str,
        )
        with open(str(logs / "overrides.log"), "a") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 - evidence is best-effort, never a blocker
        pass
