"""Whole-set convergence: diff a desired-state document against this instance.

This is the seam a deployment repo (home-tech) reconciles through: one document
declares the ENTIRE intended state — which optional core services are enabled
and the complete Layer 2 service set — and ``syrvis stack apply --from`` makes
the instance match it, including removing services that are no longer declared
(under an explicit, safe-by-default deletion policy).

The desired document (YAML):

    version: 1
    stack:                       # optional core services (primordial are always on)
      cloudflared: {enabled: true}
      dashboard:   {enabled: true, subdomain: dash}
    services:                    # the COMPLETE Layer 2 set (image-first entries)
      cyberquill:
        image: ghcr.io/acme/cyberquill:1.4.0
        subdomain: bbq
        exposure: tunnel
        port: 8300
        environment: ["KEY=VALUE", ...]      # optional
    on_undeclared: stop          # stop | remove | purge — what happens to an
                                 # installed service ABSENT from this doc.
                                 # Default 'stop' (never destructive by default).

Design rules:
- Pure plan/apply split: :func:`build_plan` is read-only and returns a JSON-ready
  plan; :func:`apply_plan` executes one. A dry-run is therefore side-effect-free
  by construction.
- ONE reconciler (phase 3 unification): the ``services:`` section is a
  PROJECTION onto ``config/services.d/`` — applying it syncs the declarations
  (write / update / disable / delete per ``on_undeclared``) and then runs the
  exact same :mod:`syrviscore.services_d` engine `syrvis reconcile` uses. The
  two declarative planes can no longer diverge, because there is only one.
- A document WITHOUT a ``services:`` key does not manage Layer 2 at all — a
  core-stack-only doc can never stop or prune your services.
- Destructive actions are marked ``destructive: true`` in the plan so callers
  (CLI confirm, MCP two-call handshake) can gate them.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import exposure as exposure_mod
from . import stack as stack_mod
from .errors import SyrvisError
from .service_manager import ServiceManager
from .service_schema import validate_service_name

DESIRED_SCHEMA_VERSION = 1

ALLOWED_TOP_LEVEL = frozenset({"version", "stack", "services", "on_undeclared"})
ON_UNDECLARED = ("stop", "remove", "purge")

# Per-service keys accepted in a desired doc entry (image-first vocabulary +
# the services.d orchestration keys).
ALLOWED_SERVICE_KEYS = frozenset(
    {
        "image",
        "subdomain",
        "exposure",
        "port",
        "environment",
        "description",
        "enabled",
        "critical",
    }
)


class ConvergeError(SyrvisError):
    """The desired-state document is invalid or a convergence step failed."""

    code = "converge_invalid"


def load_desired(path: Path) -> Dict[str, Any]:
    """Load and strictly validate a desired-state document."""
    if not path.exists():
        raise ConvergeError("Desired-state file not found: {}".format(path))
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConvergeError("Invalid YAML in {}: {}".format(path, exc))
    return validate_desired(data)


def validate_desired(data: Any) -> Dict[str, Any]:
    """Validate the desired document's shape (strict: unknown keys rejected)."""
    if not isinstance(data, dict):
        raise ConvergeError("Desired state must be a mapping")

    unknown = set(data.keys()) - ALLOWED_TOP_LEVEL
    if unknown:
        raise ConvergeError(
            "Unknown keys in desired state: {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_TOP_LEVEL))
            )
        )

    if data.get("version", DESIRED_SCHEMA_VERSION) != DESIRED_SCHEMA_VERSION:
        raise ConvergeError(
            "Unsupported desired-state version {!r} (supported: {})".format(
                data.get("version"), DESIRED_SCHEMA_VERSION
            )
        )

    on_undeclared = data.get("on_undeclared", "stop")
    if on_undeclared not in ON_UNDECLARED:
        raise ConvergeError(
            "on_undeclared must be one of {} (got {!r})".format(
                ", ".join(ON_UNDECLARED), on_undeclared
            )
        )

    stack_section = data.get("stack") or {}
    if not isinstance(stack_section, dict):
        raise ConvergeError("'stack' must be a mapping of core-service settings")
    for name, entry in stack_section.items():
        if name not in stack_mod.ALL_SERVICES:
            raise ConvergeError(
                "Unknown core service {!r} in stack (known: {})".format(
                    name, ", ".join(stack_mod.ALL_SERVICES)
                )
            )
        if not isinstance(entry, dict):
            raise ConvergeError("stack.{} must be a mapping".format(name))
        if name in stack_mod.PRIMORDIAL and entry.get("enabled") is False:
            raise ConvergeError("'{}' is primordial and cannot be disabled".format(name))

    # A doc WITHOUT a `services:` key does not manage Layer 2 at all (so a
    # core-stack-only doc can never stop/prune anything). A PRESENT key —
    # even an empty mapping — claims the complete set.
    manages_services = "services" in data
    services = data.get("services") or {}
    if not isinstance(services, dict):
        raise ConvergeError("'services' must be a mapping of Layer 2 services")
    for name, entry in services.items():
        validate_service_name(name, "service name")
        if not isinstance(entry, dict):
            raise ConvergeError("services.{} must be a mapping".format(name))
        unknown = set(entry.keys()) - ALLOWED_SERVICE_KEYS
        if unknown:
            raise ConvergeError(
                "services.{}: unknown keys {} (allowed: {})".format(
                    name, ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_SERVICE_KEYS))
                )
            )
        if not entry.get("image"):
            raise ConvergeError("services.{}: 'image' is required".format(name))
        exposure_mod.normalize(entry.get("exposure"))  # raises on invalid

    return {
        "version": DESIRED_SCHEMA_VERSION,
        "stack": stack_section,
        "services": services,
        "manages_services": manages_services,
        "on_undeclared": on_undeclared,
    }


def _doc_declarations(desired: Dict[str, Any]):
    """Materialize the doc's ``services:`` entries as full declarations.

    Uses the same builder as ``syrvis service declare``, so the doc entry and
    the flag vocabulary produce byte-identical intent.
    """
    from . import services_d

    declarations = {}
    for name, entry in (desired.get("services") or {}).items():
        declarations[name] = services_d.build_declaration(
            name,
            entry["image"],
            subdomain=entry.get("subdomain"),
            exposure=entry.get("exposure"),
            port=int(entry.get("port", 80)),
            environment=entry.get("environment"),
            description=entry.get("description", ""),
            enabled=bool(entry.get("enabled", True)),
            critical=bool(entry.get("critical", False)),
        )
    return declarations


def _effective_declarations(desired: Dict[str, Any], current_declarations):
    """The services.d state applying this doc would produce.

    Doc entries win; declarations ABSENT from the doc follow ``on_undeclared``:
    ``stop`` keeps them declared-but-off (enabled: false), ``remove``/``purge``
    drop the declaration (their installs are then pruned by the engine).
    Also returns the declaration-sync actions the apply step must perform.
    """
    import copy

    doc_declarations = _doc_declarations(desired)
    policy = desired.get("on_undeclared", "stop")

    sync_actions: List[Dict[str, Any]] = []
    effective = dict(doc_declarations)

    for name, declared in doc_declarations.items():
        existing = current_declarations.get(name)
        if existing is None:
            sync_actions.append({"kind": "declare", "name": name, "destructive": False})
        elif existing.to_dict() != declared.to_dict():
            sync_actions.append({"kind": "declare_update", "name": name, "destructive": False})

    for name, existing in current_declarations.items():
        if name in doc_declarations:
            continue
        if policy == "stop":
            softened = copy.copy(existing)
            softened.enabled = False
            effective[name] = softened
            if existing.enabled:
                sync_actions.append({"kind": "declare_disable", "name": name, "destructive": False})
        else:
            # remove/purge: intent is deleted; the reconcile prune (destructive,
            # gated) handles the installed artifacts.
            sync_actions.append({"kind": "declare_delete", "name": name, "destructive": False})

    return effective, sync_actions, doc_declarations


def build_plan(desired: Dict[str, Any], manager: Optional[ServiceManager] = None) -> Dict[str, Any]:
    """Diff desired vs actual into an ordered, JSON-ready action plan (read-only).

    The Layer 2 portion is computed by the ONE reconcile engine
    (:func:`syrviscore.services_d.build_reconcile_plan`) against the
    declarations this doc would produce — `stack apply --from` and
    `syrvis reconcile` can never plan different outcomes for the same intent.
    """
    from . import services_d

    manager = manager or ServiceManager()
    actions: List[Dict[str, Any]] = []

    # --- Core stack enablement ---
    current_stack = stack_mod.load_stack()
    for name, entry in (desired.get("stack") or {}).items():
        want_enabled = bool(entry.get("enabled", True))
        settings = {k: v for k, v in entry.items() if k != "enabled"}
        is_enabled = current_stack.is_enabled(name)
        settings_changed = any(
            current_stack.setting(name, key) != value for key, value in settings.items()
        )
        if want_enabled != is_enabled or (want_enabled and settings_changed):
            actions.append(
                {
                    "kind": "stack_enable" if want_enabled else "stack_disable",
                    "service": name,
                    "settings": settings,
                    "destructive": False,
                }
            )

    # --- Layer 2: a projection onto services.d, via the one engine ---
    reconcile_plan = None
    declaration_dump: Dict[str, Any] = {}
    if desired.get("manages_services"):
        current_declarations, _invalid = services_d.load_declarations(manager.syrvis_home)
        effective, sync_actions, _doc = _effective_declarations(desired, current_declarations)
        policy = desired.get("on_undeclared", "stop")
        reconcile_plan = services_d.build_reconcile_plan(
            manager, effective, invalid=[], prune=policy
        )
        actions.extend(sync_actions)
        actions.extend(reconcile_plan["actions"])
        # Serialize the effective declarations so apply_plan is self-contained
        # (pure plan/apply split survives a plan crossing a process boundary).
        declaration_dump = {name: svc.to_dict() for name, svc in effective.items()}

    return {
        "changed": bool(actions),
        "actions": actions,
        "manages_services": bool(desired.get("manages_services")),
        "declarations": declaration_dump,
        "reconcile": (
            {k: v for k, v in reconcile_plan.items() if k != "actions"} if reconcile_plan else None
        ),
        "summary": {
            "total": len(actions),
            "destructive": sum(1 for a in actions if a["destructive"]),
        },
    }


def apply_plan(
    plan: Dict[str, Any], manager: Optional[ServiceManager] = None
) -> List[Dict[str, Any]]:
    """Execute a plan's actions in order.

    Stack actions route through ``stack.set_enabled``; declaration-sync actions
    write/adjust ``services.d`` intent; every converge action is executed by
    :func:`syrviscore.services_d.apply_reconcile_plan` — the same engine as
    ``syrvis reconcile``. One failure never masks later actions.
    """
    from . import services_d
    from .service_schema import ServiceDefinition

    manager = manager or ServiceManager()
    results: List[Dict[str, Any]] = []

    declarations = {
        name: ServiceDefinition.from_dict(data)
        for name, data in (plan.get("declarations") or {}).items()
    }

    def record(action: Dict[str, Any], ok: bool, message: str, changed: bool = True) -> None:
        results.append(
            {
                "kind": action["kind"],
                "name": action.get("name") or action.get("service"),
                "ok": ok,
                "message": message,
                "changed": ok and changed,
            }
        )

    reconcile_actions: List[Dict[str, Any]] = []
    for action in plan.get("actions", []):
        kind = action["kind"]
        try:
            if kind in ("stack_enable", "stack_disable"):
                stack_mod.set_enabled(
                    action["service"],
                    kind == "stack_enable",
                    settings=action.get("settings") or None,
                )
                record(action, True, "{}d".format(kind.replace("_", " ")))
            elif kind in ("declare", "declare_update"):
                path = services_d.write_declaration(
                    manager.syrvis_home, declarations[action["name"]]
                )
                record(action, True, "declaration written: {}".format(path))
            elif kind == "declare_disable":
                services_d.set_declared_enabled(manager.syrvis_home, action["name"], False)
                record(action, True, "declaration set enabled: false")
            elif kind == "declare_delete":
                removed = services_d.remove_declaration(manager.syrvis_home, action["name"])
                record(action, True, "declaration removed" if removed else "no declaration")
            else:
                # Everything else is a reconcile-engine action; batch them so
                # the engine runs them with its own per-service isolation.
                reconcile_actions.append(action)
        except Exception as exc:  # noqa: BLE001 - report, don't mask later actions
            record(action, False, str(exc), changed=False)

    if reconcile_actions:
        results.extend(
            services_d.apply_reconcile_plan(manager, declarations, {"actions": reconcile_actions})
        )

    return results


def converge(
    desired_path: Path,
    dry_run: bool = False,
    manager: Optional[ServiceManager] = None,
) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]]]:
    """Convenience wrapper: load + plan (+ apply unless dry_run).

    Returns (plan, results); results is None for a dry run.
    """
    desired = load_desired(Path(desired_path))
    plan = build_plan(desired, manager=manager)
    if dry_run:
        return plan, None
    return plan, apply_plan(plan, manager=manager)
