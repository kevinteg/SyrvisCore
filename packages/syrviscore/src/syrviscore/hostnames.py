"""The *required external state* for a SyrvisCore instance.

For every hostname this instance routes through Traefik, report its exposure and
the concrete record a deployment must create so the outside world can reach it.
This is the declarative seam a config repo (e.g. home-tech) reconciles against —
it reads the report and drives DNS / Cloudflare into that state via its own MCP
tooling. SyrvisCore itself never touches DNS or the Cloudflare API; it only
declares what it routes and how each host is meant to be reached:

- ``internal`` -> a LAN DNS **A** record ``<host> -> TRAEFIK_IP``.
- ``tunnel``   -> a Cloudflare Tunnel public hostname + an Access policy, and a
  proxied **CNAME** ``<host>`` at the tunnel.

Every source of routed hostnames is enumerated: the primordial core UIs, the
optional dashboard, any enabled Synology services, and every Layer 2 service.
Each source is best-effort — a missing/unreadable one degrades to fewer entries,
never an exception.

Kept import-light and Python 3.8-clean.
"""

from typing import Any, Dict, List, Optional

from . import exposure as exposure_mod
from .config_reader import read_config

# Primordial core UIs Traefik always routes, at fixed subdomains (mirrors the
# compose labels + the dashboard's launcher links).
_PRIMORDIAL_UIS = (
    ("portainer", "portainer"),
    ("traefik", "traefik"),
)


def _host(subdomain: str, domain: str) -> str:
    return "{}.{}".format(subdomain, domain) if domain else subdomain


def _record(host: str, exp: str, traefik_ip: Optional[str]) -> Dict[str, Any]:
    """The external record a host needs, given its exposure."""
    if exp == exposure_mod.TUNNEL:
        return {
            "type": "CNAME",
            "name": host,
            "target": None,  # the deployment fills in the tunnel hostname
            "proxied": True,
            "note": "Cloudflare Tunnel public hostname + Access policy",
        }
    return {
        "type": "A",
        "name": host,
        "target": traefik_ip,
        "proxied": False,
        "note": "LAN DNS record pointing at Traefik",
    }


def build_report(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the external-state report for this instance.

    Never raises: a config that can't be read yields an empty report with an
    ``error`` field (same contract as the dashboard's read-only endpoints).
    """
    try:
        cfg = read_config(env_path=env_path)
    except Exception as exc:  # noqa: BLE001 - SYRVIS_HOME unresolved, etc.
        return {"domain": None, "traefik_ip": None, "entries": [], "error": str(exc)}

    domain = cfg.domain or ""
    traefik_ip = (cfg.values.get("TRAEFIK_IP") or "").strip() or None
    enabled_components = cfg.enabled_components or {}
    entries: List[Dict[str, Any]] = []

    def add(service: str, kind: str, subdomain: str, exp: str, enabled: bool = True) -> None:
        exp = exposure_mod.normalize(exp)
        host = _host(subdomain, domain)
        entries.append(
            {
                "service": service,
                "kind": kind,
                "subdomain": subdomain,
                "hostname": host,
                "exposure": exp,
                "enabled": bool(enabled),
                "access_required": exp == exposure_mod.TUNNEL,
                "record": _record(host, exp, traefik_ip),
            }
        )

    # Stack settings carry per-service exposure (and the dashboard subdomain).
    stack = None
    try:
        from . import stack as stack_mod

        stack = stack_mod.load_stack()
    except Exception:  # noqa: BLE001
        stack = None

    def stack_exposure(name: str) -> str:
        if stack is None:
            return exposure_mod.DEFAULT
        return str(stack.setting(name, "exposure", exposure_mod.DEFAULT))

    # 1) Primordial core UIs (always routed).
    for name, subdomain in _PRIMORDIAL_UIS:
        add(name, "core", subdomain, stack_exposure(name))

    # 2) Optional dashboard (only when enabled in the stack).
    if stack is not None and stack.is_enabled("dashboard"):
        subdomain = (
            stack.setting("dashboard", "subdomain")
            or cfg.values.get("DASHBOARD_SUBDOMAIN")
            or "dash"
        )
        add("dashboard", "core", str(subdomain), stack_exposure("dashboard"))

    # 3) Enabled Synology services. Enablement + per-service exposure both come
    # from the parsed runtime config (SYNOLOGY_<KEY>_EXPOSURE overrides default).
    try:
        from .traefik_config import SYNOLOGY_SERVICES

        for key, conf in SYNOLOGY_SERVICES.items():
            if not enabled_components.get("synology_{}".format(key)):
                continue
            exp = cfg.values.get("SYNOLOGY_{}_EXPOSURE".format(key.upper()), exposure_mod.DEFAULT)
            add("synology_{}".format(key), "synology", conf["subdomain"], exp)
    except Exception:  # noqa: BLE001
        pass

    # 4) Layer 2 services (each carries its own subdomain + exposure).
    try:
        from .service_manager import ServiceManager

        for info in ServiceManager().list():
            subdomain = info.get("subdomain")
            if not subdomain:
                continue
            add(
                info.get("name", "service"),
                "service",
                subdomain,
                info.get("exposure") or exposure_mod.DEFAULT,
                enabled=info.get("status") == "running",
            )
    except Exception:  # noqa: BLE001
        pass

    return {"domain": domain or None, "traefik_ip": traefik_ip, "entries": entries}
