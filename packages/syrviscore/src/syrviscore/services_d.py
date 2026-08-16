"""Declarative service loading: the ``config/services.d/`` reconciler (phase 1).

``services.d/`` holds one validated ``syrvis-service.yaml`` declaration per file
(filename must equal ``name:``) — the *intent* every driver writes: home-tech's
IaC via rsync+ssh, the CLI's ``service run/add`` (which dual-write here), the
dashboard, and the MCP. ``syrvis reconcile`` converges the instance to it.

Failure isolation is the design's load-bearing requirement, and it is structural:

- LOAD:     every file parses/validates independently; a bad file marks only
            that service ``invalid`` — every other file proceeds.
- CONVERGE: every service converges independently (each is its own compose
            project); one failure is recorded and the loop continues.
- HEALTH:   a failing ``critical: true`` service makes the reconcile exit
            non-zero; a non-critical failure is reported but never fatal
            (``--strict`` promotes any failure to fatal; ``--boot`` demotes all).

Safety: installed-but-undeclared services are reported ``unmanaged`` and NEVER
touched unless an explicit prune policy (stop|remove|purge) is requested, and
destructive prune actions are flagged for caller-side confirmation gating.

See docs/service-loading-design.md for the full design.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .errors import SyrvisError
from .service_schema import ServiceDefinition, load_service_definition

DECLARATIONS_DIRNAME = "services.d"

PRUNE_POLICIES = ("stop", "remove", "purge")

# Keys that never affect the container itself — excluded when diffing a
# declaration against the installed manifest, so an orchestration-only change
# (e.g. flipping `critical`) can never trigger a container replace.
_ORCHESTRATION_KEYS = ("enabled", "critical")


class ReconcileError(SyrvisError):
    """A reconcile-level failure (not a per-service one — those are isolated)."""

    code = "reconcile_failed"


def get_declarations_dir(syrvis_home: Path) -> Path:
    return Path(syrvis_home) / "config" / DECLARATIONS_DIRNAME


def declaration_path(syrvis_home: Path, name: str) -> Path:
    return get_declarations_dir(syrvis_home) / "{}.yaml".format(name)


def load_declarations(
    syrvis_home: Path,
    tolerant: bool = False,
) -> Tuple[Dict[str, ServiceDefinition], List[Dict[str, str]]]:
    """Load every ``services.d/*.yaml`` with per-file failure isolation.

    Returns:
        (valid, invalid): ``valid`` maps name -> ServiceDefinition; ``invalid``
        is a list of ``{"file", "error"}`` rows — a broken file never blocks
        the others (the design's core requirement).

    ``tolerant`` (READ-ONLY callers only, e.g. the dashboard): before parsing,
    drop any TOP-LEVEL key this reader's schema doesn't recognise, so a
    declaration written for a NEWER schema field than this (possibly older,
    image-baked) reader knows still loads for display instead of being flagged
    "invalid". A real error (bad value on a known key, name mismatch) still
    surfaces. The strict default is for the deploy/reconcile path, which must
    NEVER silently ignore an unaudited key — that rejection is the trust boundary.
    """
    directory = get_declarations_dir(syrvis_home)
    valid: Dict[str, ServiceDefinition] = {}
    invalid: List[Dict[str, str]] = []
    if not directory.exists():
        return valid, invalid

    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("declaration must be a mapping")
            if tolerant:
                data = _drop_unknown_top_level_keys(data)
            service = ServiceDefinition.from_dict(data)
            if service.name != path.stem:
                raise ValueError(
                    "declares name {!r} — it must match its filename".format(service.name)
                )
            valid[service.name] = service
        except Exception as exc:  # noqa: BLE001 - isolation: report, keep loading
            invalid.append({"file": path.name, "error": str(exc)})
    return valid, invalid


def _drop_unknown_top_level_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``data`` minus top-level keys outside the current schema allowlist.

    Lets a READ-ONLY reader tolerate a declaration written for a newer schema (a
    field added after this reader was built) — the unknown field is simply not
    shown. NEVER call this on the deploy/install path: there, ``from_dict``'s
    rejection of an unaudited key is a deliberate trust boundary.
    """
    from .service_schema import ALLOWED_TOP_LEVEL_KEYS

    return {k: v for k, v in data.items() if k in ALLOWED_TOP_LEVEL_KEYS}


def _content_dict(service: ServiceDefinition) -> Dict[str, Any]:
    """The container-affecting content of a definition (orchestration stripped)."""
    data = service.to_dict()
    for key in _ORCHESTRATION_KEYS:
        data.pop(key, None)
    return data


def _installed_manifests(manager) -> Dict[str, ServiceDefinition]:
    """Installed services by name; unloadable manifests surface as None entries."""
    installed: Dict[str, Optional[ServiceDefinition]] = {}
    services_dir = manager.services_dir
    if not services_dir.exists():
        return installed
    for service_dir in sorted(services_dir.iterdir()):
        if not service_dir.is_dir():
            continue
        manifest = service_dir / "syrvis-service.yaml"
        try:
            # A dir with NO manifest (crash mid-install, stray leftovers) is a
            # broken install: report it as present-but-unloadable so a matching
            # declaration plans a REPLACE instead of an ADD that would refuse
            # on the existing directory forever.
            installed[service_dir.name] = (
                load_service_definition(manifest) if manifest.exists() else None
            )
        except Exception:  # noqa: BLE001 - broken install -> replace candidate
            installed[service_dir.name] = None
    return installed


def _vanished_home_refusal(manager, name: str) -> Optional[str]:
    """Refusal message when an app home that ONCE held content is now gone.

    The pre-flight the convergence engine never had (incident 2026-08-16): the
    planner diffs declaration against manifest and never stats the filesystem
    under ``location:``, so a vanished ``<location>/syrviscore/apps/<name>`` was
    indistinguishable from a healthy one — and the compose generator then
    MANUFACTURED the tree plus an empty ``secrets.env`` and started a container
    against it. Absent means CREATE is the correct default for a declarative
    engine; it is the wrong default for a stateful app whose data is the thing
    being converged.

    Fires only on the high-water mark (:meth:`ServiceManager.mark_home_materialized`),
    so a genuinely new service is never blocked. Returns ``None`` when the home
    is fine, when nothing was ever recorded, or when the recorded state cannot
    be resolved (the ordinary per-service failure paths handle that, loudly).
    """
    try:
        state = manager.read_home_state(name)
    except Exception:  # noqa: BLE001 - a state read must never break planning
        return None
    if not state.get("home_materialized"):
        return None
    try:
        home = manager._app_home(name)
    except Exception:  # noqa: BLE001 - tampered manifest: the per-service path fails closed
        return None
    if home is None:
        # The manifest no longer resolves a home (removed location, unreadable
        # manifest); fall back to the path the mark itself recorded.
        home_str = state.get("home")
        if not home_str:
            return None
        home = Path(home_str)
    if manager._dir_nonempty(home):
        return None
    return (
        "app home {} has vanished (it previously held this app's data) — refusing "
        "to re-scaffold it and start {!r} against an empty tree. Almost certainly "
        "the volume root was RENAMED, not lost: run `ls -d /volume*/syrviscore*` "
        "and look for a 'syrviscore_1' sibling (DSM renames a volume root whose "
        "name collides with a shared folder at the first cold boot). Move it back "
        "before reconciling; nothing here will do it for you.".format(home, name)
    )


def build_reconcile_plan(
    manager,
    declarations: Dict[str, ServiceDefinition],
    invalid: List[Dict[str, str]],
    prune: Optional[str] = None,
) -> Dict[str, Any]:
    """Diff declared intent against installed/running state (read-only).

    Action kinds: ``add`` (materialize + start), ``replace`` (content differs;
    data dir preserved), ``start`` (declared, matching, not running), ``stop``
    (declared with ``enabled: false`` but running), ``blocked`` (a safety
    refusal — see :func:`_vanished_home_refusal`; never converted into any other
    action), and — only under an explicit prune policy — ``prune_stop`` /
    ``prune_remove`` / ``prune_purge`` for installed services with no
    declaration.
    """
    if prune is not None and prune not in PRUNE_POLICIES:
        raise ReconcileError(
            "prune policy must be one of {} (got {!r})".format(", ".join(PRUNE_POLICIES), prune)
        )

    actions: List[Dict[str, Any]] = []
    in_sync: List[str] = []
    disabled_ok: List[str] = []
    installed = _installed_manifests(manager)

    # FLOOR CHECK (incident 2026-08-16). "Zero declarations" is a legitimate
    # state only for an instance with nothing installed. Zero declarations while
    # services ARE installed means the config tree is not where we are looking —
    # a mis-rooted SYRVIS_HOME, an unmounted volume, a renamed install root — and
    # `load_declarations` returns empty-and-valid for a missing directory, so the
    # engine would otherwise report a clean, converged, empty world. Refuse
    # instead: an empty config is never a reason to act on a populated instance.
    #
    # Scoped to the AMBIENT path (no prune policy) on purpose. That is the one
    # the boot hook, cron and every unattended reconcile take, and the one whose
    # emptiness is an inference. An explicit `--prune`/`on_undeclared: remove`
    # is an operator INSTRUCTION carrying its own policy word — it is not
    # "planning nothing", it is planning exactly what was asked, and it is
    # destructive-token-gated over the seam.
    if prune is None and installed and not declarations:
        raise ReconcileError(
            "0 declarations but {} installed service(s) — refusing to reconcile "
            "against an empty config/services.d. Check that SYRVIS_HOME points at "
            "the real install root (`ls -d /volume*/syrviscore*`) and that "
            "{} exists.".format(len(installed), get_declarations_dir(manager.syrvis_home))
        )

    for name, declared in declarations.items():
        current = installed.get(name)
        status = manager._get_service_status(declared.container_name or name)

        if not declared.enabled:
            # Declared-but-off: stop anything alive (running, restarting,
            # paused, created — a crash-looping container is NOT stopped);
            # never materialize. Reached BEFORE the vanished-home pre-flight on
            # purpose: stopping is always safe, and a service on its way off is
            # not a service to block.
            if name in installed and status not in ("stopped", "exited", "unknown"):
                actions.append(
                    {
                        "kind": "stop",
                        "name": name,
                        "image": declared.image,
                        "critical": declared.critical,
                        "destructive": False,
                    }
                )
            else:
                disabled_ok.append(name)
            continue

        # PRE-FLIGHT, ahead of every add/replace/start decision: a stateful app
        # whose home has vanished must be BLOCKED, not converged.
        refusal = _vanished_home_refusal(manager, name)
        if refusal:
            actions.append(
                {
                    "kind": "blocked",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                    "message": refusal,
                }
            )
            continue

        if name not in installed:
            actions.append(
                {
                    "kind": "add",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                }
            )
        elif current is None or _content_dict(current) != _content_dict(declared):
            actions.append(
                {
                    "kind": "replace",
                    "name": name,
                    # from_image = what's installed now, image = what it becomes —
                    # so the plan shows the version transition (e.g. a digest bump).
                    "from_image": current.image if current is not None else None,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,  # data dir is preserved across replace
                }
            )
        elif status != "running" or manager.is_service_flapping(declared.container_name or name):
            # FLAPPING counts as not-in-sync (incident 2026-08-16). A
            # `restart: unless-stopped` container reads "running" between
            # crashes, so a crash-looping service was classed in_sync and
            # reconcile declared victory over six of them for ~15 minutes. The
            # emitted action is the ordinary `start` — an idempotent `up -d`
            # that also re-materializes compose (repairing host-side dir/perm
            # drift, a real cause of crash loops). It deliberately does NOT
            # force-recreate: repeatedly killing a container that may be slowly
            # recovering is worse than reporting the truth and letting an
            # operator run `syrvis service recreate`.
            actions.append(
                {
                    "kind": "start",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                    "flapping": status == "running",
                }
            )
        else:
            in_sync.append(name)

    unmanaged = sorted(set(installed) - set(declarations))
    if prune:
        for name in unmanaged:
            actions.append(
                {
                    "kind": "prune_{}".format(prune),
                    "name": name,
                    "image": installed[name].image if name in installed else None,
                    "critical": False,
                    # stop is reversible; remove drops config (data kept); purge drops data
                    "destructive": prune != "stop",
                }
            )

    return {
        "changed": bool(actions),
        "actions": actions,
        "in_sync": in_sync,
        "disabled": disabled_ok,
        "unmanaged": unmanaged,
        "invalid": invalid,
        "summary": {
            "declared": len(declarations),
            "invalid": len(invalid),
            "total_actions": len(actions),
            "destructive": sum(1 for a in actions if a["destructive"]),
        },
    }


def apply_reconcile_plan(
    manager,
    declarations: Dict[str, ServiceDefinition],
    plan: Dict[str, Any],
    trigger: str = "reconcile",
    allow_halted: bool = False,
) -> List[Dict[str, Any]]:
    """Execute a reconcile plan with per-service failure isolation.

    Every action reports its own outcome; a failure never stops later actions.
    ``trigger`` is threaded into the deployment-history records the manager
    verbs write ("reconcile", "converge", ...). While the instance is HALTED
    (graceful shutdown), applying is refused — a cron/MCP reconcile must not
    restart just-stopped workloads; ``syrvis resume`` passes ``allow_halted``.
    Planning (:func:`build_reconcile_plan`) stays pure and always allowed.
    """
    from . import lifecycle

    lifecycle.guard_not_halted("reconcile", allow_halted=allow_halted)

    results: List[Dict[str, Any]] = []

    for action in plan.get("actions", []):
        kind, name = action["kind"], action["name"]
        try:
            if kind == "blocked":
                # A planner-level SAFETY REFUSAL, never an instruction. It is
                # deliberately handled first and can never fall through into
                # add/start: the whole point is that converging this service
                # would destroy or shadow its data (incident 2026-08-16). It
                # reports as a failed action, so a critical service's refusal
                # fails the reconcile instead of passing silently.
                ok, msg = False, action.get("message") or "blocked"
            elif kind == "add":
                ok, msg = manager.install_declaration(
                    declarations[name], start=True, trigger=trigger
                )
            elif kind == "replace":
                # design/26: a location change on an installed service that
                # still has data is REFUSED before anything is torn down — a
                # naive replace would materialize an EMPTY home at the new
                # location (a DB would re-init: presents as data loss). The
                # refusal surfaces as this service's own failed action; the
                # rest of the plan proceeds (failure isolation). The bypass is
                # the app-move procedure (empty/absent old data root proceeds).
                refusal = manager._location_change_refusal(name, declarations[name].location)
                if refusal:
                    ok, msg = False, refusal
                else:
                    # keep_declaration=True also keeps the history silent: the
                    # reinstall below records the ONE logical deploy (with the
                    # image transition), never a remove+add pair.
                    # fire_hooks=False for the same reason — a replace fires
                    # one pre/post-deploy pair (from the reinstall), never a
                    # spurious stop quiesce.
                    ok, msg = manager.remove(
                        name, purge=False, keep_declaration=True, fire_hooks=False
                    )
                    if ok:
                        # The data dir predates this replace: a failed
                        # re-install must roll back the new artifacts WITHOUT
                        # destroying it.
                        ok, msg = manager.install_declaration(
                            declarations[name],
                            start=True,
                            preserve_data_on_rollback=True,
                            trigger=trigger,
                            previous_image=action.get("from_image"),
                        )
            elif kind == "start":
                ok, msg = manager.start(name)
            elif kind == "stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_remove":
                ok, msg = manager.remove(name, purge=False, trigger=trigger)
            elif kind == "prune_purge":
                ok, msg = manager.remove(name, purge=True, trigger=trigger)
            else:
                ok, msg = False, "unknown action kind {!r}".format(kind)
        except Exception as exc:  # noqa: BLE001 - isolation: record, continue
            ok, msg = False, str(exc)
        results.append(
            {
                "kind": kind,
                "name": name,
                "ok": ok,
                "critical": action.get("critical", False),
                "message": msg,
            }
        )

    # Any applied action changed the deployed image set; drop the updates cache
    # so a just-reconciled service stops showing an "available" update it no
    # longer has (its "current" is frozen in that cache otherwise).
    if any(r["ok"] for r in results):
        from . import image_updates

        image_updates.invalidate_cache()

    return results


def verdict(
    plan: Dict[str, Any],
    results: Optional[List[Dict[str, Any]]],
    strict: bool = False,
) -> Tuple[bool, str]:
    """(ok, reason) for the exit decision.

    Defaults:
    - An INVALID declaration file is fatal: corruption of intent must never
      pass silently (a truncated critical service's file has no readable
      ``critical`` flag, so criticality cannot exempt it). Isolation is
      preserved regardless — every other service was still converged.
    - A FAILED action is fatal only for a ``critical: true`` service;
      non-critical failures degrade but never block the rest.
    ``strict`` promotes any failure to fatal. ``--boot`` callers ignore the
    verdict entirely (best-effort).
    """
    failures = [r for r in (results or []) if not r["ok"]]
    invalid = plan.get("invalid") or []

    if strict and (failures or invalid):
        return False, "{} invalid declaration(s), {} failed action(s) (strict)".format(
            len(invalid), len(failures)
        )

    if invalid:
        return False, "invalid declaration(s): {}".format(", ".join(row["file"] for row in invalid))

    critical_failures = [r for r in failures if r.get("critical")]
    if critical_failures:
        return False, "critical service(s) failed: {}".format(
            ", ".join(r["name"] for r in critical_failures)
        )
    return True, "ok"


def adopt(manager, name: str) -> Path:
    """Generate a declaration from an existing install (migration helper)."""
    manifest = manager.services_dir / name / "syrvis-service.yaml"
    if not manifest.exists():
        raise ReconcileError("Service '{}' is not installed (nothing to adopt)".format(name))
    try:
        service = load_service_definition(manifest)  # validates before we bless it
    except Exception as exc:  # noqa: BLE001 - typed error for per-row isolation
        raise ReconcileError("Cannot adopt '{}': {}".format(name, exc))
    return write_declaration(manager.syrvis_home, service)


def build_declaration(
    name: str,
    image: str,
    subdomain: Optional[str] = None,
    exposure: Optional[str] = None,
    port: int = 80,
    environment: Optional[List[str]] = None,
    description: str = "",
    enabled: bool = True,
    critical: bool = False,
) -> ServiceDefinition:
    """Author a declaration from image-first vocabulary (the trust boundary applies).

    The builder behind ``syrvis service declare`` and the MCP ``service_declare``
    tool: it only AUTHORS intent — nothing is installed or started until a
    reconcile applies it.
    """
    from . import exposure as exposure_mod
    from .service_manager import _image_tag  # lazy: service_manager imports us

    manifest: Dict[str, Any] = {
        "name": name,
        "version": _image_tag(image),
        "image": image,
        "traefik": {
            "enabled": True,
            "subdomain": (subdomain or name).strip().lower(),
            "port": port,
            "exposure": exposure_mod.normalize(exposure),
        },
        "enabled": enabled,
        "critical": critical,
    }
    if description:
        manifest["description"] = description
    if environment:
        manifest["environment"] = list(environment)
    return ServiceDefinition.from_dict(manifest)


def write_declaration(syrvis_home: Path, service: ServiceDefinition) -> Path:
    """Persist a declaration file for ``service`` verbatim (orchestration kept)."""
    from .service_schema import dump_definition

    path = declaration_path(syrvis_home, service.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return dump_definition(service, path, include_orchestration=True)


def write_declaration_from_install(syrvis_home: Path, service: ServiceDefinition) -> Path:
    """The dual-write used by imperative installs/updates.

    Writes the service CONTENT while preserving the operator's orchestration
    keys from any existing declaration — a git/catalog manifest can therefore
    never set or reset ``enabled``/``critical``; only the operator (editing the
    declaration) or the reconcile layer owns them.
    """
    existing_path = declaration_path(syrvis_home, service.name)
    enabled, critical = True, False
    if existing_path.exists():
        try:
            existing = ServiceDefinition.from_dict(yaml.safe_load(existing_path.read_text()))
            enabled, critical = existing.enabled, existing.critical
        except Exception:  # noqa: BLE001 - unreadable prior declaration: defaults
            pass
    import copy

    to_write = copy.copy(service)
    to_write.enabled = enabled
    to_write.critical = critical
    return write_declaration(syrvis_home, to_write)


def remove_declaration(syrvis_home: Path, name: str) -> bool:
    """Delete a declaration (imperative `service remove` must not leave intent
    behind, or the next reconcile would resurrect the service)."""
    path = declaration_path(syrvis_home, name)
    if path.exists():
        path.unlink()
        return True
    return False


def set_declared_enabled(syrvis_home: Path, name: str, enabled: bool) -> bool:
    """Flip ``enabled`` on an existing declaration (imperative start/stop as
    file authors). Returns False when no declaration exists (nothing to edit).
    """
    path = declaration_path(syrvis_home, name)
    if not path.exists():
        return False
    try:
        service = ServiceDefinition.from_dict(yaml.safe_load(path.read_text()))
    except Exception:  # noqa: BLE001 - don't let a broken file block stop/start
        return False
    if service.enabled == enabled:
        # No-op flips never rewrite the file: reconcile's own start actions and
        # repeated stops must not churn (re-serialize/re-chmod/re-own) the
        # IaC-authored declarations they were planned from.
        return True
    service.enabled = enabled
    write_declaration(syrvis_home, service)
    return True
