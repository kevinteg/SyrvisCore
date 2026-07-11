"""Declarative core-tier stack for a SyrvisCore instance.

``config/stack.yaml`` declares WHICH base-tier containers this instance runs. It
is the "declarative intent" surface for the core tier (the Layer 2 equivalent is
``syrvis-service.yaml``). The compose generator reads it; ``syrvis start`` brings
up whatever is declared.

- **Primordial** (``traefik``, ``portainer``) — always on: the routing substrate
  and a fallback management UI. They cannot be disabled.
- **Optional** (``cloudflared``, ``dashboard``, ``cloudflare_ddns``) — opt-in.

Secrets and network settings stay in ``.env``; this file holds only enablement
plus a few generation-time knobs (e.g. the dashboard subdomain).

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import paths
from .errors import SyrvisError

STACK_SCHEMA_VERSION = 1

PRIMORDIAL = ("traefik", "portainer")
OPTIONAL = ("cloudflared", "dashboard", "cloudflare_ddns")
ALL_SERVICES = PRIMORDIAL + OPTIONAL

# Optional services whose ``.env`` token, when present, means "already configured".
# Used by the migration fallback + `stack list` hints.
TOKEN_FOR = {
    "cloudflared": "CLOUDFLARE_TUNNEL_TOKEN",
    "cloudflare_ddns": "CLOUDFLARE_API_TOKEN",
}

# The container name each stack service maps to (for matching running containers).
CONTAINER_NAME = {
    "traefik": "traefik",
    "portainer": "portainer",
    "cloudflared": "cloudflared",
    "dashboard": "syrviscore-dashboard",
    "cloudflare_ddns": "cloudflare-ddns",
}


class StackError(SyrvisError):
    """Raised on an invalid stack operation (unknown/primordial service, bad file)."""

    code = "stack_invalid"


@dataclass
class StackService:
    name: str
    enabled: bool
    settings: Dict[str, object] = field(default_factory=dict)


@dataclass
class Stack:
    services: Dict[str, StackService]

    def is_enabled(self, name: str) -> bool:
        svc = self.services.get(name)
        return bool(svc and svc.enabled)

    def enabled_services(self) -> List[str]:
        return [n for n in ALL_SERVICES if self.is_enabled(n)]

    def setting(self, name: str, key: str, default: object = None) -> object:
        svc = self.services.get(name)
        if not svc:
            return default
        return svc.settings.get(key, default)

    def to_dict(self) -> dict:
        out_services = {}
        for name in ALL_SERVICES:
            svc = self.services.get(name)
            if not svc:
                continue
            entry = {"enabled": svc.enabled}
            entry.update(svc.settings)
            out_services[name] = entry
        return {"version": STACK_SCHEMA_VERSION, "services": out_services}

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), default_flow_style=False, sort_keys=False)


def _svc(name: str, enabled: bool, **settings: object) -> StackService:
    return StackService(name=name, enabled=enabled, settings=dict(settings))


def default_stack() -> Stack:
    """Fresh-install posture: primordial on, everything optional off (opt-in)."""
    services = {n: _svc(n, True) for n in PRIMORDIAL}
    services["cloudflared"] = _svc("cloudflared", False)
    services["dashboard"] = _svc("dashboard", False, subdomain="dash")
    services["cloudflare_ddns"] = _svc("cloudflare_ddns", False)
    return Stack(services=services)


def infer_stack_from_env() -> Stack:
    """Migration fallback when no ``stack.yaml`` exists yet.

    Preserves pre-stack behavior: cloudflared was emitted whenever configured, so
    enable it here (its runtime token still gates whether it works); DDNS was
    token-gated; the dashboard is new, so it stays opt-in (off).
    """
    stack = default_stack()
    stack.services["cloudflared"].enabled = True
    if os.getenv(TOKEN_FOR["cloudflare_ddns"]):
        stack.services["cloudflare_ddns"].enabled = True
    return stack


def from_dict(data: Optional[dict]) -> Stack:
    raw = (data or {}).get("services") or {}
    services = {}
    for name in ALL_SERVICES:
        entry = raw.get(name) or {}
        enabled = bool(entry.get("enabled", name in PRIMORDIAL))
        settings = {k: v for k, v in entry.items() if k != "enabled"}
        services[name] = StackService(name=name, enabled=enabled, settings=settings)
    for n in PRIMORDIAL:  # primordial is always on regardless of the file
        services[n].enabled = True
    return Stack(services=services)


def get_stack_path() -> Path:
    return paths.get_config_dir() / "stack.yaml"


def load_stack() -> Stack:
    """Read ``config/stack.yaml``; fall back to the env-inferred migration default
    when it's absent or ``SYRVIS_HOME`` can't be resolved (e.g. in unit tests)."""
    try:
        path = get_stack_path()
    except Exception:  # noqa: BLE001 - SYRVIS_HOME unresolved
        return infer_stack_from_env()
    if not path.exists():
        return infer_stack_from_env()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise StackError("invalid stack.yaml: {}".format(exc))
    return from_dict(data)


def save_stack(stack: Stack) -> Path:
    path = get_stack_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stack.to_yaml())
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return path


def set_enabled(name: str, enabled: bool, settings: Optional[dict] = None) -> Stack:
    """Enable/disable a core service in ``stack.yaml`` and persist it."""
    if name not in ALL_SERVICES:
        raise StackError(
            "unknown core service '{}' (known: {})".format(name, ", ".join(ALL_SERVICES))
        )
    if name in PRIMORDIAL and not enabled:
        raise StackError("'{}' is primordial and cannot be disabled".format(name))

    stack = load_stack()
    svc = stack.services.get(name) or _svc(name, enabled)
    svc.enabled = enabled
    if settings:
        svc.settings.update({k: v for k, v in settings.items() if v is not None})
    stack.services[name] = svc
    save_stack(stack)
    return stack
