"""Launcher links — the one-stop-shop to the other UIs.

Derives links from config: the primordial UIs (Portainer, Traefik) always, the
enabled Synology services, and any Layer 2 services. All under the instance domain.
"""

from fastapi import APIRouter

# Both catalogs come from syrviscore.traefik_config (the single source) so the
# service→subdomain mapping cannot drift between the CLI, the hostnames report,
# the validators, and this launcher.
from syrviscore.traefik_config import PRIMORDIAL_UIS, SYNOLOGY_SERVICES

router = APIRouter(prefix="/api", tags=["links"])

# Primordial core UIs (always routed by SyrvisCore).
_PRIMORDIAL = [(ui["subdomain"], ui["label"], ui["description"]) for ui in PRIMORDIAL_UIS]

# Synology services keyed by their enabled_components flag -> (subdomain, label).
_SYNOLOGY = {
    "synology_{}".format(key): (svc["subdomain"], svc["label"])
    for key, svc in SYNOLOGY_SERVICES.items()
}


@router.get("/links")
def links() -> dict:
    """Quick-launch links to every UI this instance exposes."""
    try:
        from syrviscore.config_reader import read_config

        cfg = read_config()
    except Exception as exc:  # noqa: BLE001
        return {"domain": None, "links": [], "error": str(exc)}

    domain = cfg.domain
    enabled = cfg.enabled_components or {}
    items = []

    def add(subdomain, name, description, category):
        if domain:
            items.append(
                {
                    "name": name,
                    "url": "https://{}.{}".format(subdomain, domain),
                    "description": description,
                    "category": category,
                }
            )

    for sub, name, desc in _PRIMORDIAL:
        add(sub, name, desc, "primordial")

    for key, (sub, name) in _SYNOLOGY.items():
        if enabled.get(key):
            add(sub, name, "Synology", "synology")

    # Layer 2 services (best-effort — each carries its own url)
    try:
        from syrviscore.service_manager import ServiceManager

        for svc in ServiceManager().list():
            if svc.get("url"):
                items.append(
                    {
                        "name": svc.get("name", "service"),
                        "url": svc["url"],
                        "description": svc.get("description", ""),
                        "category": "service",
                    }
                )
    except Exception:  # noqa: BLE001
        pass

    return {"domain": domain, "links": items}
