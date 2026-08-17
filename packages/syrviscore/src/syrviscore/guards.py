"""Refusal guards for mutating verbs — "not while THAT is true".

Two guards, both born from the same 2026-08-16 review finding: the platform had
exactly three narrow refusals and none of them covered the two things that
actually go wrong when an operator (or an agent) acts on a homebase that is
already in a deliberate, degraded state.

``guard_enable_change``
    A GitOps apply carries the repo's ``enabled:`` flags. When a service was
    deliberately stopped, the repo still says ``enabled: true``, so the apply
    silently re-enables it and the next converge starts it. Fourteen services
    were resurrected that way, into a seven-day array resync, by a runbook
    whose own text asserted they would stay down. The guard names every service
    whose declared intent the write would FLIP from off to on and refuses
    without ``--allow-enable-change`` — the exact sibling of the
    ``--allow-secret-change`` rotation guard, which already proved the pattern
    is accepted here.

``guard_bulk_degraded``
    A bulk converge issues image pulls and container recreates. Run against an
    array that is mid-resync, that competes with the rebuild for the same
    spindles — and nothing measures it: the platform's daemon breaker watches
    *dockerd* latency, not array latency, so it never trips. The guard reads
    ``/proc/mdstat`` (one file, no daemon, no network) and requires ``--force``
    while any array is resyncing/recovering/reshaping.

Both are OVERRIDABLE by design — the operator legitimately needs to move data
during a resync, and legitimately needs to re-enable a service. What is not
optional is EVIDENCE: every override writes a line to ``logs/overrides.log``
via :func:`syrviscore.intent.log_override`, so the bypass is as visible as the
refusal would have been.
"""

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import intent as intent_mod
from .errors import SyrvisError

MDSTAT_PATH = "/proc/mdstat"

# `md6 : active raid5 sata1p3[0] sata2p3[1] ...`
_MD_HEADER_RE = re.compile(r"^(md\d+)\s*:")
# `      [==>..................]  resync = 12.7% (...) finish=9999.9min speed=11000K/sec`
_MD_PROGRESS_RE = re.compile(
    r"\b(resync|recovery|reshape|check|repair)\s*=\s*([0-9.]+)%.*?"
    r"(?:finish\s*=\s*(\S+))?\s*(?:speed\s*=\s*(\S+))?$"
)

#: Operations that mean "this array is already spending its IO budget".
#: ``check``/``repair`` (a scrub) count: DSM runs them for days on a rackmount
#: array and they starve a converge exactly as a rebuild does.
BUSY_OPERATIONS = ("resync", "recovery", "reshape", "check", "repair")


class EnableChangeRefused(SyrvisError):
    """A write would re-enable a deliberately-stopped service."""

    code = "enable_change_refused"


class BulkDegradedRefused(SyrvisError):
    """A bulk mutating verb was refused while an array is rebuilding."""

    code = "bulk_degraded_refused"


# ---------------------------------------------------------------------------
# guard_enable_change
# ---------------------------------------------------------------------------


def guard_enable_change(
    services: Iterable[str],
    allow: bool = False,
    home: Optional[Path] = None,
    action: str = "apply",
) -> List[str]:
    """Refuse (or journal) a write that would flip services from off to on.

    ``services`` is the already-computed list of names whose DECLARED intent
    goes ``enabled: false`` -> ``enabled: true``. Computing it is the caller's
    job — only the caller knows what it is about to write — so this stays a
    pure policy function with one job: refuse by name, or record the override.

    Returns the list it was given (so a caller can report it either way).
    """
    names = sorted(set(services))
    if not names:
        return names
    if not allow:
        raise EnableChangeRefused(
            "refusing to re-enable {} deliberately-stopped service(s): {}. "
            "These are declared off on this instance and the incoming set would "
            "start them. Lift the intent first (`syrvis service unshed -- <name>`) "
            "or, if the enablement is what you mean, re-run with "
            "--allow-enable-change.".format(len(names), ", ".join(names))
        )
    intent_mod.log_override(
        home, action, "--allow-enable-change", {"services": names, "count": len(names)}
    )
    return names


def enable_changes(
    incoming: Dict[str, bool],
    live: Dict[str, Optional[bool]],
    shed: Optional[Iterable[str]] = None,
) -> List[str]:
    """Names that go declared-off -> declared-on, excluding the shed set.

    ``live`` maps a name to its currently-declared ``enabled`` (``None`` for a
    service with no declaration yet — a first write is never an enable CHANGE).
    Shed services are excluded because they are handled more strongly upstream:
    the overlay pins them ``enabled: false`` in what actually gets written, so
    they are not a refusal, they are a correction.
    """
    shed_set = set(shed or ())
    return sorted(
        name
        for name, want in incoming.items()
        if want and name not in shed_set and live.get(name) is False
    )


# ---------------------------------------------------------------------------
# guard_bulk_degraded
# ---------------------------------------------------------------------------


def parse_mdstat(text: str) -> List[Dict[str, Any]]:
    """Arrays with an in-flight rebuild, from ``/proc/mdstat`` text. Pure.

    mdstat is a stanza format: an ``mdN :`` header line, then continuation
    lines, one of which carries the progress bar when (and only when) an
    operation is running. So the parse tracks the current array name and
    attributes any progress line to it. An array with no progress line is not
    reported at all — absence of the line IS the healthy state.
    """
    arrays: List[Dict[str, Any]] = []
    current: Optional[str] = None
    for raw in (text or "").splitlines():
        header = _MD_HEADER_RE.match(raw.strip())
        if header:
            current = header.group(1)
            continue
        if current is None:
            continue
        match = _MD_PROGRESS_RE.search(raw.strip())
        if not match:
            continue
        operation = match.group(1)
        if operation not in BUSY_OPERATIONS:
            continue
        arrays.append(
            {
                "array": current,
                "operation": operation,
                "percent": float(match.group(2)),
                "finish": match.group(3) or None,
                "speed": match.group(4) or None,
            }
        )
    return arrays


def busy_arrays(mdstat_path: str = MDSTAT_PATH) -> List[Dict[str, Any]]:
    """Rebuilding arrays on this host — ``[]`` when mdstat is absent/unreadable.

    Unreadable must mean "no evidence of a rebuild", never "assume one": this
    guard sits in front of ordinary operational verbs, and a platform that
    refuses to converge because it could not open a file is worse than one that
    converges during a rebuild. The signal is advisory; the refusal it drives
    is overridable.
    """
    try:
        return parse_mdstat(Path(mdstat_path).read_text())
    except Exception:  # noqa: BLE001 - no mdstat (macOS, container, DSM quirk)
        return []


def describe_arrays(arrays: List[Dict[str, Any]]) -> str:
    parts = []
    for row in arrays:
        detail = "{} {} {:.1f}%".format(row["array"], row["operation"], row["percent"])
        if row.get("finish"):
            detail += ", finish={}".format(row["finish"])
        if row.get("speed"):
            detail += ", speed={}".format(row["speed"])
        parts.append(detail)
    return "; ".join(parts)


def guard_bulk_degraded(
    action: str,
    force: bool = False,
    home: Optional[Path] = None,
    mdstat_path: str = MDSTAT_PATH,
    services: Optional[Iterable[str]] = None,
    by: str = "cli:force",
) -> Optional[List[Dict[str, Any]]]:
    """Refuse (or journal) a bulk mutating verb while an array is rebuilding.

    Returns the busy-array rows when there ARE any (so the caller can report
    the override it just made), else ``None``. ``services`` is the optional
    list of workloads the verb would touch — it is named in the refusal, in the
    journal line, and in the breaker reset, because "which services was this
    override for" is the first question anyone asks afterwards.

    ``--force`` is OPERATOR INTENT, so it also **closes the breakers in its
    scope** (design/60 §11.1 point 5 and point 6's "a close closes all"): a
    human who has looked at a rebuilding array and said "do it anyway" is
    exactly the signal the breaker was holding out for, and leaving it armed
    would make the next automatic pass skip the work the operator just
    authorized. ``by`` carries WHICH surface said so — only ``cli:``/``seam:``/
    ``mcp:`` values close, and the store fails safe on anything else.
    """
    arrays = busy_arrays(mdstat_path)
    if not arrays:
        return None
    names = sorted(set(services or ()))
    if not force:
        raise BulkDegradedRefused(
            "refusing '{}' while the storage is rebuilding ({}). A bulk converge "
            "pulls images and recreates containers against the same spindles the "
            "rebuild is using, and nothing in the platform measures array latency. "
            "{}Wait for the rebuild, or re-run with --force (the override is "
            "recorded).".format(
                action,
                describe_arrays(arrays),
                "Affected: {}. ".format(", ".join(names)) if names else "",
            )
        )
    cleared = close_breakers_in_scope(home, names, by=by)
    intent_mod.log_override(
        home,
        action,
        "--force",
        {
            "arrays": arrays,
            "services": names,
            "breakers_closed": [r["context"] for r in cleared],
        },
    )
    return arrays


def close_breakers_in_scope(
    home: Optional[Path], services: Iterable[str], by: str = "cli:force"
) -> List[Dict[str, Any]]:
    """Close every plane's breaker for ``services`` — the operator reset path.

    One helper so every overriding verb resets the same way (design/60 §11.1
    point 6: ONE reset path, so ``deploy-stack --only X`` and ``syrvis up`` stop
    being half-measures that each leave the other's breaker armed).

    ``by`` decides, and it FAILS SAFE: only ``cli:``/``seam:``/``mcp:`` values
    represent operator intent and close anything; ``hostd``/``s99``/``cron`` (and
    an absent value) inherit the breakers instead — so a scheduled job that
    passes ``--force`` cannot quietly reset a breaker a human never saw
    (``opc:F2``). Best-effort and non-raising: a reset must never be the thing
    that fails the verb the operator already authorized. Returns the rows that
    were actually live.
    """
    names = sorted(set(services or ()))
    if not names:
        return []
    try:
        from . import breakers

        if not breakers.closes_breakers(by):
            return []
        return breakers.close_scope(home, names, by=by)
    except Exception:  # noqa: BLE001 - a reset never blocks the verb
        return []
