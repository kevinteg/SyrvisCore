"""Workload lifecycle: derived states, transition hooks, and the instance runstate.

Three loosely-coupled concerns, deliberately small (start simple):

**States.** A workload's state is DERIVED, never stored: a pure function of the
observed container/VM status and the declared intent. Four states —
``running``, ``stopped``, ``failed``, ``unknown``. ``failed`` means
needs-attention: restarting/dead always, exited only when the workload is
declared enabled (a deliberately-stopped service is ``stopped``, not a page).

**Hooks.** Transitions fire hooks, two kinds honoring derive-not-declare:

- *Container hooks* — the manifest's ``hooks:`` block maps an event to a
  DECLARED task (the audited ``tasks:`` argv, docker exec in the service's own
  container). Valid only for ``pre-stop`` and ``post-start`` (the two events
  where the container is verifiably running). This is the DB-quiesce path.
- *Host hooks* — one root-owned executable per event under
  ``$SYRVIS_HOME/hooks.d/<workload>/<event>`` (or ``hooks.d/instance/<event>``
  for instance events). Arbitrary root code, so the trust check is the whole
  point: the leaf AND every parent directory up to ``hooks.d/`` must be
  root-owned, non-group/world-writable, non-symlink — an unprivileged operator
  cannot author one. Insecure or absent hooks are skipped (fail-open: the
  control is write-inability, not runtime blocking).

Failure policy: only ``pre-start``/``pre-deploy`` may abort a transition (and
never under ``force`` — the UPS path). Stop/shutdown/post-* hooks log and
proceed: you must always be able to stop.

**Runstate.** One persisted instance state: ``active`` (no file) or ``halted``
(``data/state/runstate.json``, root-written, 0644, no secrets). ``syrvis
shutdown`` writes it FIRST (closing the race with a cron reconcile that would
restart just-stopped containers), stops everything in order, and records the
outcome; ``syrvis resume`` (or a UPS-reason boot) clears it. ``reason=ups``
means resume on next boot; ``reason=maintenance`` survives reboots until an
explicit resume.

Kept import-light and Python 3.8-clean (runs on the DSM 3.8 CLI).
"""

import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .errors import SyrvisError

# ---------------------------------------------------------------------------
# Derived workload states
# ---------------------------------------------------------------------------

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"


def derive_state(observed: str, enabled: Optional[bool]) -> str:
    """Map an observed docker status + declared intent to a lifecycle state.

    ``enabled=None`` means intent unknown — it suppresses ``failed`` for an
    exited container (don't page on ambiguity) but never for restarting/dead.
    """
    o = (observed or "").strip().lower()
    if o == "running":
        return STATE_RUNNING
    if o in ("restarting", "dead"):
        return STATE_FAILED
    if o == "exited":
        return STATE_FAILED if enabled else STATE_STOPPED
    if o in ("created", "paused", "removing", "stopped"):
        return STATE_STOPPED
    return STATE_UNKNOWN


def derive_vm_state(power: str, enabled: Optional[bool]) -> str:
    """Map a VMM power state + declared intent to a lifecycle state."""
    p = (power or "").strip().lower()
    if p in ("running", "transition"):
        return STATE_RUNNING
    if p == "stopped":
        return STATE_FAILED if enabled else STATE_STOPPED
    return STATE_UNKNOWN  # not_found / unknown / backend unreachable


# ---------------------------------------------------------------------------
# Hook events
# ---------------------------------------------------------------------------

HOST_HOOK_EVENTS = frozenset(
    {
        "pre-start",
        "post-start",
        "pre-stop",
        "post-stop",
        "pre-deploy",
        "post-deploy",
        "pre-shutdown",
        "post-resume",
    }
)
# docker exec needs a verifiably-running container: only these two qualify.
CONTAINER_HOOK_EVENTS = frozenset({"pre-stop", "post-start"})
# Only bring-up gates may abort (and never under force). Stop always proceeds.
ABORTING_EVENTS = frozenset({"pre-start", "pre-deploy"})

HOOK_DEFAULT_TIMEOUT_S = 30
HOOK_OUTPUT_CAP = 16 * 1024

INSTANCE_SCOPE = "instance"


@dataclass
class HookRun:
    """The outcome of one hook invocation (container or host)."""

    kind: str  # "container" | "host"
    event: str
    scope: str  # "service" | "vm" | "instance"
    workload: str
    ran: bool = False
    ok: bool = True
    skipped_reason: Optional[str] = None
    rc: Optional[int] = None
    duration_s: float = 0.0
    output_tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "event": self.event,
            "scope": self.scope,
            "workload": self.workload,
            "ran": self.ran,
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "rc": self.rc,
            "duration_s": round(self.duration_s, 2),
        }


def get_hooks_dir(home: Path) -> Path:
    return paths.get_hooks_dir(home)


def _owner_ok(uid: int) -> bool:
    """Root must own hooks in production; under sim/non-root dev the current
    user stands in (they could not create root-owned files to test with)."""
    if os.geteuid() == 0:
        return uid == 0
    return uid == os.geteuid()


def _hook_is_trusted(path: Path, hooks_root: Path) -> Tuple[bool, Optional[str]]:
    """The load-bearing check: only root-authored, root-controlled hooks run.

    Walks from ``hooks_root`` down to the leaf; every component must be a
    non-symlink owned by root (production) and not group/world-writable; the
    leaf must be a regular file with owner-exec.
    """
    components: List[Path] = [hooks_root]
    relative = path.relative_to(hooks_root)
    current = hooks_root
    for part in relative.parts:
        current = current / part
        components.append(current)

    for component in components:
        try:
            st = os.lstat(str(component))
        except OSError as exc:
            return False, "unreadable: {}".format(exc)
        if stat.S_ISLNK(st.st_mode):
            return False, "{} is a symlink".format(component.name)
        if not _owner_ok(st.st_uid):
            return False, "{} not owned by root".format(component.name)
        if st.st_mode & 0o022:
            return False, "{} is group/world-writable".format(component.name)

    leaf = os.lstat(str(path))
    if not stat.S_ISREG(leaf.st_mode):
        return False, "not a regular file"
    if not leaf.st_mode & 0o100:
        return False, "not owner-executable"

    # Belt-and-braces after the symlink-free walk: the resolved leaf must
    # still live under hooks.d (defense in depth against traversal).
    real = os.path.realpath(str(path))
    root_real = os.path.realpath(str(hooks_root))
    if not real.startswith(root_real + os.sep):
        return False, "escapes hooks.d"
    return True, None


def _log_hook(home: Path, run: HookRun) -> None:
    """Append one structured line to logs/hooks.log (best-effort)."""
    try:
        logs = Path(home) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            dict(run.to_dict(), at=_utc_now(), output_tail=run.output_tail[-1024:]),
            default=str,
        )
        with open(str(logs / "hooks.log"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_host_hook(
    home: Path,
    event: str,
    scope: str,
    workload: str = "",
    reason: str = "",
    context: Optional[Dict[str, str]] = None,
    timeout: int = HOOK_DEFAULT_TIMEOUT_S,
) -> HookRun:
    """Run ``hooks.d/<workload or instance>/<event>`` if present and trusted.

    Absent hook -> silent no-op (ok). Insecure hook -> skipped with a warning
    reason (ok — fail-open; the control is that an operator cannot WRITE one).
    Failure/timeout -> ran, not ok; the CALLER decides whether that aborts.
    """
    directory_name = workload if scope != INSTANCE_SCOPE else INSTANCE_SCOPE
    run = HookRun(kind="host", event=event, scope=scope, workload=directory_name)

    if os.environ.get("SYRVIS_IN_HOOK"):
        return run  # re-entrancy fuse: a hook calling syrvis fires no hooks

    try:
        home = Path(home) if home is not None else paths.get_syrvis_home()
    except Exception:  # noqa: BLE001 - no resolvable home: hooks are dormant
        return run
    hooks_root = get_hooks_dir(home)
    path = hooks_root / directory_name / event
    if not path.exists() and not os.path.islink(str(path)):
        return run

    trusted, why = _hook_is_trusted(path, hooks_root)
    if not trusted:
        run.skipped_reason = why
        _log_hook(Path(home), run)
        return run

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SYRVIS_HOME": str(home),
        "SYRVIS_HOOK_EVENT": event,
        "SYRVIS_HOOK_SCOPE": scope,
        "SYRVIS_HOOK_WORKLOAD": workload,
        "SYRVIS_HOOK_REASON": reason,
        "SYRVIS_IN_HOOK": "1",
    }
    for key, value in (context or {}).items():
        env["SYRVIS_HOOK_{}".format(key.upper())] = str(value)

    started = time.monotonic()
    run.ran = True
    try:
        result = subprocess.run(
            [str(path)],
            cwd=str(home),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        run.rc = result.returncode
        run.ok = result.returncode == 0
        run.output_tail = ((result.stdout or "") + (result.stderr or ""))[-HOOK_OUTPUT_CAP:]
    except subprocess.TimeoutExpired:
        run.ok = False
        run.output_tail = "timed out after {}s".format(timeout)
    except OSError as exc:
        run.ok = False
        run.output_tail = str(exc)
    run.duration_s = time.monotonic() - started
    _log_hook(Path(home), run)
    return run


# ---------------------------------------------------------------------------
# Instance runstate (active | halted)
# ---------------------------------------------------------------------------

RUNSTATE_SCHEMA = "syrvis-runstate/v1"
# ups     = a power event (UPS on battery): come back when power does.
# reboot  = a DSM-initiated shutdown/reboot (home-tech design/28 rc.d-stop hook):
#           auto-resume on the next boot, same as ups.
# maintenance = an operator hold: stay down across reboots until 'syrvis resume'.
HALT_REASONS = ("ups", "reboot", "maintenance")
# Reasons whose default boot policy is to auto-resume when the machine comes back
# up (overridable via the explicit resume_on_boot arg / --hold).
AUTO_RESUME_REASONS = ("ups", "reboot")


class InstanceHaltedError(SyrvisError):
    """A bring-up/converge was refused because the instance is halted."""

    code = "instance_halted"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_runstate(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The halted record, or None when active (absent/unreadable == active)."""
    try:
        path = paths.get_runstate_path(home)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("state") == "halted":
            return data
    except Exception:  # noqa: BLE001 - unreadable state fails safe to active
        pass
    return None


def is_halted(home: Optional[Path] = None) -> bool:
    return read_runstate(home) is not None


def guard_not_halted(action: str, allow_halted: bool = False, home: Optional[Path] = None) -> None:
    """Raise InstanceHaltedError for bulk bring-up while the instance is halted."""
    if allow_halted:
        return
    state = read_runstate(home)
    if state is not None:
        raise InstanceHaltedError(
            "instance is halted (reason: {}, since {}) — run 'syrvis resume' "
            "before '{}'".format(state.get("reason", "?"), state.get("at", "?"), action)
        )


def write_halted(
    home: Optional[Path],
    reason: str,
    workloads: List[Dict[str, str]],
    by: str = "shutdown",
    resume_on_boot: Optional[bool] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist the halted state atomically (root 0644; names only, no secrets)."""
    if reason not in HALT_REASONS:
        raise SyrvisError("halt reason must be one of {}".format(", ".join(HALT_REASONS)))
    record = {
        "schema": RUNSTATE_SCHEMA,
        "state": "halted",
        "reason": reason,
        "at": _utc_now(),
        "by": by,
        # ups/reboot auto-resume when the machine comes back up; maintenance
        # stays down across reboots until an explicit resume (see HALT_REASONS).
        "resume_on_boot": (
            (reason in AUTO_RESUME_REASONS) if resume_on_boot is None else bool(resume_on_boot)
        ),
        "workloads": workloads,
    }
    if result is not None:
        record["result"] = result
    path = paths.get_runstate_path(home)
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
        path.chmod(0o644)
    except OSError:
        pass
    return record


def clear_runstate(home: Optional[Path] = None) -> bool:
    """Remove the halted record (absence == active). True if one was removed."""
    path = paths.get_runstate_path(home)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# Instance orchestration: shutdown / resume / graceful restart
# ---------------------------------------------------------------------------

DEFAULT_SHUTDOWN_BUDGET_S = 180
DEFAULT_VM_DEADLINE_S = 90
CORE_STOP_ORDER = ("syrviscore-dashboard", "cloudflared", "portainer", "traefik")

# Schema defaults for a service that declares no `shutdown:` block.
DEFAULT_STOP_TIMEOUT_S = 30
DEFAULT_STOP_PRIORITY = 50

# RESERVE-FIRST CLAMPING (design/63 D6, dep:F3; interim until 63 M1's graph).
#
# The walk stops in ascending priority bands and used to spend the budget
# FRONT-TO-BACK: `min(declared, remaining)`. So the stores — which stop LAST, by
# design, and are the only workloads for which the grace period is load-bearing
# — got whatever the consumers had not already spent, squeezed toward the 5s
# floor. design/63 measured the arithmetic from the live declarations and it does
# not fit: store wave 120s + VM ACPI drain 90s + core 30s = 240s of irreducible
# reserve against a `timeout 150s` rc.d wrapper. The doctrine that falls out of
# that is one sentence: **a consumer force-killed at 60s is an inconvenience, a
# postgres force-killed at 5s is a WAL replay or worse** — so the reserve is held
# back FIRST and the consumers clamp into what is left, never the reverse.
#
# A "store" is a service in the STORE band or above; the band convention is
# design/25 D4's (20 consumer / 50 default / 70 cache+broker / 90 store). A
# deployment that declares no band ≥ STORE_BAND simply reserves nothing for L2
# and behaves as before — the arithmetic is driven by declarations, not by names.
STORE_BAND = 90
# The whole core phase (dashboard → cloudflared → portainer → traefik). They are
# routing/UI containers that stop promptly; 30s is design/63 D6's figure for the
# phase, not a per-container timeout (that stays capped at 30s below).
CORE_STOP_RESERVE_S = 30
# Never clamp a stop below this: SIGTERM plus a heartbeat to react to it.
STOP_FLOOR_S = 5


@dataclass
class _Phase:
    name: str
    ok: bool = True
    items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"phase": self.name, "ok": self.ok, "items": self.items}


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def shutdown_instance(
    home: Path,
    reason: str = "maintenance",
    budget_s: int = DEFAULT_SHUTDOWN_BUDGET_S,
    vm_deadline_s: Optional[int] = None,
    resume_on_boot: Optional[bool] = None,
    by: str = "cli",
    service_manager=None,
    docker_manager=None,
    vm_manager=None,
) -> Dict[str, Any]:
    """Gracefully stop every managed workload and mark the instance halted.

    Order (chosen for a dying UPS battery): write halted FIRST (closes the
    reconcile race) → instance pre-shutdown hook → issue VM ACPI shutdown
    (guests drain in the background) → stop L2 services in priority bands
    (pre-stop hooks quiesce DBs, then ``compose stop -t <grace>``) → wait for
    VMs, hard-off stragglers → stop core with Traefik last. Nothing aborts on
    a single failure; the report enumerates every outcome. Declared intent
    (services.d enabled flags) is NEVER touched — this is an operational stop,
    not a change of intent, and resume/reconcile restores everything.

    ``budget_s`` is spent RESERVE-FIRST (design/63 D6, dep:F3): the stores' own
    declared grace, the VM drain and the core phase are held back before the
    first consumer is stopped, and consumers clamp into what remains (floor
    :data:`STOP_FLOOR_S`). The arithmetic is reported under ``budget`` so a
    5-second consumer stop is explainable rather than mysterious.
    """
    from .service_manager import ServiceManager

    sm = service_manager or ServiceManager(syrvis_home=home)
    deadline = time.monotonic() + budget_s
    started_at = _utc_now()
    phases: List[_Phase] = []
    failures: List[str] = []
    forced: List[str] = []

    # --- snapshot what is running (before anything stops) -------------------
    snapshot: List[Dict[str, str]] = []
    l2_rows: List[Dict[str, Any]] = []
    try:
        for row in sm.list():
            if (row.get("status") or "").lower() in ("running", "restarting"):
                l2_rows.append(row)
                snapshot.append({"name": row["name"], "scope": "service", "state": "running"})
    except Exception:  # noqa: BLE001 - docker down: nothing to stop at L2
        pass

    vm_rows: List[Dict[str, Any]] = []
    if vm_manager is not None:
        vms = vm_manager
    else:
        try:
            from .vms import VmManager

            vms = VmManager(home=home)
        except Exception:  # noqa: BLE001
            vms = None
    if vms is not None:
        try:
            for row in vms.list():
                if row.get("power") == "running":
                    vm_rows.append(row)
                    snapshot.append({"name": row["name"], "scope": "vm", "state": "running"})
        except Exception:  # noqa: BLE001 - VMM unreachable: skip the tier
            pass

    core_names: List[str] = []
    dm = docker_manager
    if dm is None:
        try:
            from .docker_manager import DockerManager

            dm = DockerManager()
        except Exception:  # noqa: BLE001 - docker unreachable
            dm = None
    if dm is not None:
        try:
            for container in dm.get_core_containers():
                if container.status == "running":
                    core_names.append(container.name)
                    snapshot.append({"name": container.name, "scope": "core", "state": "running"})
        except Exception:  # noqa: BLE001
            pass

    # --- halt gate up FIRST: a cron reconcile must not fight the stops ------
    write_halted(home, reason, snapshot, by=by, resume_on_boot=resume_on_boot)

    # --- instance pre-shutdown hook (never aborts) --------------------------
    pre = _Phase("pre_shutdown_hook")
    hook = run_host_hook(home, "pre-shutdown", INSTANCE_SCOPE, reason=reason)
    pre.ok = hook.ok
    pre.items.append(hook.to_dict())
    phases.append(pre)

    # --- issue VM graceful shutdown (fire-and-forget; guests drain).
    # VmManager.stop fires the vm pre-stop host hook itself.
    vm_phase = _Phase("vms")
    vm_windows = _vm_windows(vms, vm_rows, vm_deadline_s)
    vm_issued_at = time.monotonic()
    # Guests drain CONCURRENTLY with the L2 stops below (the ACPI action is
    # issued here and waited on after), so the drain's claim on the budget is
    # anchored at the ISSUE, decays in real time, and is a max — not a sum.
    vm_drain_until = (vm_issued_at + max(vm_windows.values())) if vm_windows else 0.0
    for row in vm_rows:
        name = row["name"]
        try:
            vms.stop(name)
            vm_phase.items.append(
                {
                    "name": name,
                    "action": "acpi-shutdown",
                    "window_s": vm_windows.get(name, DEFAULT_VM_DEADLINE_S),
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate; try the rest
            vm_phase.ok = False
            failures.append("vm:{}".format(name))
            vm_phase.items.append({"name": name, "error": str(exc)})

    # --- L2 services, ascending priority bands (DBs last within L2) ---------
    # Reserve-first: the stores' declared grace + what the VM drain and the core
    # phase still need is held back, and every EARLIER service clamps into the
    # remainder (see STORE_BAND above). Declarations are read ONCE here — the old
    # sort key re-parsed each manifest on every comparison.
    l2_phase = _Phase("services")
    timeouts = {r["name"]: _l2_stop_timeout(sm, r["name"]) for r in l2_rows}
    priorities = {r["name"]: _l2_priority(sm, r["name"]) for r in l2_rows}
    ordered = sorted(l2_rows, key=lambda r: (priorities[r["name"]], r["name"]))
    core_reserve = CORE_STOP_RESERVE_S if core_names else 0
    store_reserve = sum(timeouts[r["name"]] for r in ordered if priorities[r["name"]] >= STORE_BAND)
    budget_report = {
        "budget_s": budget_s,
        "store_reserve_s": store_reserve,
        "vm_reserve_s": int(max(vm_windows.values())) if vm_windows else 0,
        "core_reserve_s": core_reserve,
        "reserve_s": int(store_reserve + (max(vm_windows.values()) if vm_windows else 0))
        + core_reserve,
        "clamped": [],
    }
    store_tail = store_reserve
    for row in ordered:
        name = row["name"]
        declared = timeouts[name]
        is_store = priorities[name] >= STORE_BAND
        if is_store:
            # THE RESERVE ITSELF. A store keeps its declared stop_timeout even
            # when the budget is already overspent: a clean checkpoint is the
            # thing the whole walk exists to buy, and the container's own
            # stop_grace_period (generated from this value) holds anyway if the
            # transport kills us mid-stop.
            store_tail -= declared
            grace = declared
        else:
            allowance = _remaining(deadline) - (
                store_tail + max(0.0, vm_drain_until - time.monotonic()) + core_reserve
            )
            grace = int(min(declared, max(STOP_FLOOR_S, allowance)))
        item = {"name": name, "grace_s": grace, "declared_s": declared, "band": priorities[name]}
        if grace < declared:
            item["clamped"] = True
            budget_report["clamped"].append(name)
        ok, msg = sm.stop(
            name,
            remove=False,
            set_intent=False,
            fire_hooks=True,
            force=True,
            hook_timeout=int(min(HOOK_DEFAULT_TIMEOUT_S, max(1, _remaining(deadline)))),
            stop_timeout=grace,
        )
        item["result"] = "stopped" if ok else "failed"
        if not ok:
            l2_phase.ok = False
            failures.append("service:{}".format(name))
            item["error"] = msg
        l2_phase.items.append(item)
    phases.append(vm_phase)
    phases.append(l2_phase)

    # --- wait for VMs to power off; hard-off stragglers ---------------------
    # Each guest gets its DECLARED graceful window (vms.d stop_timeout, default
    # 90s), capped by an explicit --vm-deadline. The window is measured from the
    # ACPI ISSUE, not from the start of this wait: the guest has been draining
    # throughout the L2 phase, so re-anchoring here would double-count that time
    # (and, with the budget already spent, could truncate a drain that is nearly
    # done). The VM share is part of the reserve — the L2 walk clamped itself to
    # protect it — so it is deliberately NOT re-clamped by `_remaining` here.
    if vm_rows and vms is not None:
        deadlines = {
            row["name"]: vm_issued_at + vm_windows.get(row["name"], DEFAULT_VM_DEADLINE_S)
            for row in vm_rows
        }
        still_on = _wait_vms_off(vms, deadlines)
        for name in still_on:
            try:
                vms.stop(name, hard=True)
                forced.append("vm:{}".format(name))
            except Exception:  # noqa: BLE001
                failures.append("vm:{}".format(name))
        if still_on:
            grace = time.monotonic() + min(15, max(5, _remaining(deadline)))
            leftovers = _wait_vms_off(vms, {name: grace for name in still_on})
            for name in leftovers:
                failures.append("vm:{}".format(name))
                vm_phase.ok = False
        for item in vm_phase.items:
            if "error" not in item:
                name = item.get("name", "")
                item["result"] = "forced" if "vm:{}".format(name) in forced else "off"
        for row in vm_rows:
            run_host_hook(home, "post-stop", "vm", row["name"], reason=reason)

    # --- core stack, Traefik last -------------------------------------------
    core_phase = _Phase("core")
    if dm is not None and core_names:
        ordered_core = sorted(
            core_names,
            key=lambda n: CORE_STOP_ORDER.index(n) if n in CORE_STOP_ORDER else 1,
        )
        stop_timeout = int(min(30, max(5, _remaining(deadline))))
        for name in ordered_core:
            try:
                dm.stop_core_container(name, timeout=stop_timeout)
                core_phase.items.append({"name": name, "result": "stopped"})
            except Exception as exc:  # noqa: BLE001 - isolate per container
                core_phase.ok = False
                failures.append("core:{}".format(name))
                core_phase.items.append({"name": name, "error": str(exc)})
    phases.append(core_phase)

    finished_at = _utc_now()
    elapsed = budget_s - _remaining(deadline)
    result = {"ok": not failures, "failed": failures, "forced": forced}
    # Refresh the runstate with the final outcome (still halted).
    write_halted(home, reason, snapshot, by=by, resume_on_boot=resume_on_boot, result=result)

    return {
        "action": "shutdown",
        "reason": reason,
        "resume_on_boot": (
            (reason in AUTO_RESUME_REASONS) if resume_on_boot is None else bool(resume_on_boot)
        ),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_s": round(elapsed, 1),
        "budget_s": budget_s,
        # The reserve-first arithmetic, so an operator can see WHY a consumer
        # got 5s instead of 30 (design/63 D6).
        "budget": budget_report,
        "phases": [p.to_dict() for p in phases],
        "ok": not failures,
        "degraded": bool(failures or forced),
        "failed": failures,
        "forced": forced,
        "exit": 0 if not failures else 2,
    }


def _l2_stop_timeout(sm, name: str) -> int:
    """The service's declared shutdown.stop_timeout (default 30s)."""
    try:
        from .service_schema import load_service_definition

        svc = load_service_definition(sm.services_dir / name / "syrvis-service.yaml")
        return int((svc.shutdown or {}).get("stop_timeout", DEFAULT_STOP_TIMEOUT_S))
    except Exception:  # noqa: BLE001
        return DEFAULT_STOP_TIMEOUT_S


def _l2_priority(sm, name: str) -> int:
    """The service's declared shutdown.priority (lower stops first; default 50)."""
    try:
        from .service_schema import load_service_definition

        svc = load_service_definition(sm.services_dir / name / "syrvis-service.yaml")
        return int((svc.shutdown or {}).get("priority", DEFAULT_STOP_PRIORITY))
    except Exception:  # noqa: BLE001
        return DEFAULT_STOP_PRIORITY


def _vm_windows(vms, vm_rows: List[Dict[str, Any]], vm_deadline_s: Optional[int]) -> Dict[str, int]:
    """``{vm: graceful ACPI window}`` for the RUNNING guests (vms.d stop_timeout).

    The VM tier's claim on the shutdown budget, declared rather than assumed
    (fnd:F37/dep:F24 — the HAOS guest was an undeclared tenant of a 90s slice).
    An explicit ``--vm-deadline`` caps every guest; an undeclared/unreadable
    guest falls back to :data:`DEFAULT_VM_DEADLINE_S`.
    """
    declared: Dict[str, int] = {}
    if vms is not None:
        try:
            declared = {
                vm.name: int(getattr(vm, "stop_timeout", DEFAULT_VM_DEADLINE_S))
                for vm in vms.declarations()
            }
        except Exception:  # noqa: BLE001 - unreadable declarations: defaults
            declared = {}
    windows: Dict[str, int] = {}
    for row in vm_rows:
        window = declared.get(row["name"], DEFAULT_VM_DEADLINE_S)
        if vm_deadline_s is not None:
            window = min(window, vm_deadline_s)
        windows[row["name"]] = int(window)
    return windows


def _wait_vms_off(vms, deadlines: Dict[str, float], poll_s: float = 3.0) -> List[str]:
    """Poll VM power until each named guest is off or ITS deadline passes.

    ``deadlines`` maps name -> absolute monotonic deadline (each guest gets
    its declared graceful window). Returns the names still not off past their
    deadline. The existing VmManager.stop only ISSUES the ACPI action —
    without this wait the host could lose power mid-guest-shutdown.
    """
    remaining = dict(deadlines)
    overdue: List[str] = []
    while remaining:
        now = time.monotonic()
        for name in list(remaining):
            off = False
            try:
                status = vms.status(name)
                off = status.power in ("stopped", "not_found")
            except Exception:  # noqa: BLE001 - backend blip: keep waiting
                off = False
            if off:
                del remaining[name]
            elif now >= remaining[name]:
                overdue.append(name)
                del remaining[name]
        if remaining:
            next_deadline = min(remaining.values())
            time.sleep(min(poll_s, max(0.1, next_deadline - time.monotonic())))
    return overdue


def resume_instance(
    home: Path,
    boot: bool = False,
    by: str = "cli",
    service_manager=None,
    docker_manager=None,
    vm_manager=None,
) -> Dict[str, Any]:
    """Bring the instance back: core first, then VMs, then L2 via reconcile.

    Starts the core stack (graceful stops disarm ``restart: unless-stopped``,
    so an explicit start is REQUIRED after a power cycle), powers on the
    snapshot's VMs (or declared autostart ones), runs the ONE reconcile engine
    for L2 — the enabled flags were never touched, so everything that was
    running comes back — and only THEN clears the halted state: a crash
    mid-resume leaves the instance halted, so the next boot (or a re-run)
    still knows recovery is owed. Fires the instance ``post-resume`` hook
    last.
    """
    from . import services_d
    from .service_manager import ServiceManager

    state = read_runstate(home)
    if state is None and not boot:
        return {"action": "resume", "ok": True, "changed": False, "detail": "instance is active"}

    phases: List[_Phase] = []
    failures: List[str] = []

    # --- core first: routing substrate + networks ---------------------------
    core_phase = _Phase("core")
    dm = docker_manager
    if dm is None:
        try:
            from .docker_manager import DockerManager

            dm = DockerManager()
        except Exception as exc:  # noqa: BLE001
            dm = None
            core_phase.ok = False
            failures.append("core")
            core_phase.items.append({"error": str(exc)})
    if dm is not None:
        try:
            warnings = dm.start_core_services(allow_halted=True)
            core_phase.items.append({"result": "started", "warnings": warnings})
        except Exception as exc:  # noqa: BLE001
            core_phase.ok = False
            failures.append("core")
            core_phase.items.append({"error": str(exc)})
    phases.append(core_phase)

    # --- VMs: snapshot wins, else declared enabled+autostart ----------------
    vm_phase = _Phase("vms")
    vms = vm_manager
    if vms is None:
        try:
            from .vms import VmManager

            vms = VmManager(home=home)
        except Exception:  # noqa: BLE001
            vms = None
    if vms is not None:
        snapshot_vms = [
            w["name"] for w in (state or {}).get("workloads", []) if w.get("scope") == "vm"
        ]
        try:
            targets = snapshot_vms or [
                vm.name for vm in vms.declarations() if vm.enabled and vm.autostart
            ]
        except Exception:  # noqa: BLE001
            targets = snapshot_vms
        for name in targets:
            try:
                vms.start(name)
                vm_phase.items.append({"name": name, "result": "powering-on"})
            except Exception as exc:  # noqa: BLE001
                vm_phase.ok = False
                failures.append("vm:{}".format(name))
                vm_phase.items.append({"name": name, "error": str(exc)})
    phases.append(vm_phase)

    # --- L2 via the one reconcile engine ------------------------------------
    l2_phase = _Phase("services")
    sm = service_manager or ServiceManager(syrvis_home=home)
    try:
        declarations, invalid = services_d.load_declarations(Path(home))
        plan = services_d.build_reconcile_plan(sm, declarations, invalid)
        results = services_d.apply_reconcile_plan(
            sm, declarations, plan, trigger="resume", allow_halted=True
        )
        for row in results:
            item = {
                "name": row["name"],
                "kind": row["kind"],
                "result": "ok" if row["ok"] else "failed",
            }
            if not row["ok"]:
                l2_phase.ok = False
                failures.append("service:{}".format(row["name"]))
                item["error"] = row.get("message", "")
            l2_phase.items.append(item)
    except Exception as exc:  # noqa: BLE001
        l2_phase.ok = False
        failures.append("services")
        l2_phase.items.append({"error": str(exc)})
    phases.append(l2_phase)

    # Bring-up attempted across every tier — only now stop being "halted".
    # (Partial failures still clear: the instance is substantially up and the
    # remaining gate would block the very verbs that could finish the job.)
    clear_runstate(home)

    post = _Phase("post_resume_hook")
    hook = run_host_hook(
        home, "post-resume", INSTANCE_SCOPE, reason=(state or {}).get("reason", "")
    )
    post.ok = hook.ok
    post.items.append(hook.to_dict())
    phases.append(post)

    return {
        "action": "resume",
        "boot": boot,
        "changed": True,
        "phases": [p.to_dict() for p in phases],
        "ok": not failures,
        "failed": failures,
        "exit": 0 if not failures else 2,
    }
