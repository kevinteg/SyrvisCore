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
- Everything routes through the existing audited primitives (`stack.set_enabled`,
  `ServiceManager.add_image/remove/stop`), so the schema trust boundary and
  rollback behavior are inherited, never reimplemented.
- Destructive actions (remove/purge) are marked ``destructive: true`` in the
  plan so callers (CLI confirm, MCP two-call handshake) can gate them.
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

# Per-service keys accepted in a desired doc entry (image-first vocabulary).
ALLOWED_SERVICE_KEYS = frozenset(
    {"image", "subdomain", "exposure", "port", "environment", "description"}
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
        "on_undeclared": on_undeclared,
    }


def _current_l2_state(manager: ServiceManager) -> Dict[str, Dict[str, Any]]:
    """Installed Layer 2 services with their full effective routing.

    ``list()`` lacks port/environment, so each installed manifest is loaded for
    an exact comparison. A service whose manifest fails to load is reported
    with ``error`` (it will show as needing replacement, never silently equal).
    """
    from .services_d import _installed_manifests

    current: Dict[str, Dict[str, Any]] = {}
    for name, svc in _installed_manifests(manager).items():
        if svc is None:
            # Unloadable/manifest-less install: never silently equal -> replace.
            current[name] = {"error": "unloadable manifest"}
            continue
        current[name] = {
            "image": svc.image,
            "subdomain": svc.traefik.subdomain if svc.traefik.enabled else "",
            "exposure": svc.traefik.exposure if svc.traefik.enabled else None,
            "port": svc.traefik.port,
            "environment": sorted(svc.environment),
        }
    return current


def _desired_entry_normalized(name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "image": entry["image"],
        "subdomain": (entry.get("subdomain") or name).strip().lower(),
        "exposure": exposure_mod.normalize(entry.get("exposure")),
        "port": int(entry.get("port", 80)),
        "environment": sorted(entry.get("environment") or []),
    }


def build_plan(desired: Dict[str, Any], manager: Optional[ServiceManager] = None) -> Dict[str, Any]:
    """Diff desired vs actual into an ordered, JSON-ready action plan (read-only)."""
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

    # --- Layer 2 set ---
    current = _current_l2_state(manager)
    desired_services = {
        name: _desired_entry_normalized(name, entry)
        for name, entry in (desired.get("services") or {}).items()
    }

    for name, want in desired_services.items():
        have = current.get(name)
        if have is None:
            actions.append({"kind": "service_add", "name": name, "destructive": False, **want})
        elif have.get("error") or any(have.get(k) != want[k] for k in want):
            changes = {
                k: {"from": have.get(k), "to": want[k]} for k in want if have.get(k) != want[k]
            }
            actions.append(
                {
                    "kind": "service_replace",
                    "name": name,
                    "changes": changes,
                    "destructive": False,  # data dir is preserved across replace
                    **want,
                }
            )

    on_undeclared = desired.get("on_undeclared", "stop")
    for name in sorted(set(current) - set(desired_services)):
        kind = {
            "stop": "service_stop",
            "remove": "service_remove",
            "purge": "service_purge",
        }[on_undeclared]
        actions.append(
            {
                "kind": kind,
                "name": name,
                # stop is reversible; remove drops config (data kept); purge drops data
                "destructive": on_undeclared != "stop",
            }
        )

    return {
        "changed": bool(actions),
        "actions": actions,
        "summary": {
            "total": len(actions),
            "destructive": sum(1 for a in actions if a["destructive"]),
        },
    }


def apply_plan(
    plan: Dict[str, Any], manager: Optional[ServiceManager] = None
) -> List[Dict[str, Any]]:
    """Execute a plan's actions in order, via the existing audited primitives.

    Returns one result row per action: {kind, name, ok, message, changed}.
    Never raises mid-plan for a single action's failure — each row reports its
    own outcome so a partial failure is visible, not masking later actions.
    """
    manager = manager or ServiceManager()
    results: List[Dict[str, Any]] = []

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
            elif kind == "service_add":
                ok, msg = manager.add_image(
                    action["name"],
                    action["image"],
                    subdomain=action["subdomain"],
                    exposure=action["exposure"],
                    port=action["port"],
                    environment=action["environment"],
                    start=True,
                )
                record(action, ok, msg)
            elif kind == "service_replace":
                # Remove (data preserved, services.d declaration KEPT — losing
                # it on a failed re-add would erase intent) then re-add. The
                # data dir predates this replace, so a failed re-install must
                # roll back without destroying it.
                ok, msg = manager.remove(action["name"], purge=False, keep_declaration=True)
                if ok:
                    ok, msg = manager.add_image(
                        action["name"],
                        action["image"],
                        subdomain=action["subdomain"],
                        exposure=action["exposure"],
                        port=action["port"],
                        environment=action["environment"],
                        start=True,
                        preserve_data_on_rollback=True,
                    )
                record(action, ok, msg)
            elif kind == "service_stop":
                ok, msg = manager.stop(action["name"])
                record(action, ok, msg)
            elif kind == "service_remove":
                ok, msg = manager.remove(action["name"], purge=False)
                record(action, ok, msg)
            elif kind == "service_purge":
                ok, msg = manager.remove(action["name"], purge=True)
                record(action, ok, msg)
            else:
                record(action, False, "unknown action kind {!r}".format(kind), changed=False)
        except Exception as exc:  # noqa: BLE001 - report, don't mask later actions
            record(action, False, str(exc), changed=False)

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
