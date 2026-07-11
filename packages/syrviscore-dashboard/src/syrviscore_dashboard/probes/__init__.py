"""Component probes + the registry the aggregator fans out over.

Each entry is ``(component_name, needs_http, fn)``. HTTP probes receive a shared
``httpx.AsyncClient``; local probes (docker/config) receive only settings.
"""

from .base import ProbeResult, Status, guard, severity
from .cloudflare_ddns import probe_ddns
from .cloudflared import probe_cloudflared
from .config import probe_config
from .core import probe_core
from .portainer import probe_portainer
from .traefik import probe_traefik

PROBES = [
    ("core", False, probe_core),
    ("traefik", True, probe_traefik),
    ("portainer", True, probe_portainer),
    ("cloudflared", True, probe_cloudflared),
    ("cloudflare_ddns", True, probe_ddns),
    ("config", False, probe_config),
]

COMPONENTS = [name for name, _, _ in PROBES]

__all__ = [
    "ProbeResult",
    "Status",
    "guard",
    "severity",
    "PROBES",
    "COMPONENTS",
    "probe_core",
    "probe_traefik",
    "probe_portainer",
    "probe_cloudflared",
    "probe_ddns",
    "probe_config",
]
