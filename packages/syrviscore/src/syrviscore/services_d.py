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


def build_reconcile_plan(
    manager,
    declarations: Dict[str, ServiceDefinition],
    invalid: List[Dict[str, str]],
    prune: Optional[str] = None,
) -> Dict[str, Any]:
    """Diff declared intent against installed/running state (read-only).

    Action kinds: ``add`` (materialize + start), ``replace`` (content differs;
    data dir preserved), ``start`` (declared, matching, not running), ``stop``
    (declared with ``enabled: false`` but running), and — only under an explicit
    prune policy — ``prune_stop`` / ``prune_remove`` / ``prune_purge`` for
    installed services with no declaration.
    """
    if prune is not None and prune not in PRUNE_POLICIES:
        raise ReconcileError(
            "prune policy must be one of {} (got {!r})".format(", ".join(PRUNE_POLICIES), prune)
        )

    actions: List[Dict[str, Any]] = []
    in_sync: List[str] = []
    disabled_ok: List[str] = []
    installed = _installed_manifests(manager)

    for name, declared in declarations.items():
        current = installed.get(name)
        status = manager._get_service_status(declared.container_name or name)

        if not declared.enabled:
            # Declared-but-off: stop anything alive (running, restarting,
            # paused, created — a crash-looping container is NOT stopped);
            # never materialize.
            if name in installed and status not in ("stopped", "exited", "unknown"):
                actions.append(
                    {
                        "kind": "stop",
                        "name": name,
                        "critical": declared.critical,
                        "destructive": False,
                    }
                )
            else:
                disabled_ok.append(name)
            continue

        if name not in installed:
            actions.append(
                {
                    "kind": "add",
                    "name": name,
                    "critical": declared.critical,
                    "destructive": False,
                }
            )
        elif current is None or _content_dict(current) != _content_dict(declared):
            actions.append(
                {
                    "kind": "replace",
                    "name": name,
                    "critical": declared.critical,
                    "destructive": False,  # data dir is preserved across replace
                }
            )
        elif status != "running":
            actions.append(
                {
                    "kind": "start",
                    "name": name,
                    "critical": declared.critical,
                    "destructive": False,
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
) -> List[Dict[str, Any]]:
    """Execute a reconcile plan with per-service failure isolation.

    Every action reports its own outcome; a failure never stops later actions.
    """
    results: List[Dict[str, Any]] = []

    for action in plan.get("actions", []):
        kind, name = action["kind"], action["name"]
        try:
            if kind == "add":
                ok, msg = manager.install_declaration(declarations[name], start=True)
            elif kind == "replace":
                ok, msg = manager.remove(name, purge=False, keep_declaration=True)
                if ok:
                    # The data dir predates this replace: a failed re-install
                    # must roll back the new artifacts WITHOUT destroying it.
                    ok, msg = manager.install_declaration(
                        declarations[name], start=True, preserve_data_on_rollback=True
                    )
            elif kind == "start":
                ok, msg = manager.start(name)
            elif kind == "stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_remove":
                ok, msg = manager.remove(name, purge=False)
            elif kind == "prune_purge":
                ok, msg = manager.remove(name, purge=True)
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
