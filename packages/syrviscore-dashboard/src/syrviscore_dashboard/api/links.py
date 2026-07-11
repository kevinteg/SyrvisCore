"""Launcher links — the one-stop-shop to the other UIs.

Derives links from config: the primordial UIs (Portainer, Traefik) always, the
enabled Synology services, and any Layer 2 services. All under the instance domain.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["links"])

# Primordial core UIs (always routed by SyrvisCore).
_PRIMORDIAL = [
    ("portainer", "Portainer", "Container management"),
    ("traefik", "Traefik", "Reverse proxy dashboard"),
]

# Synology services keyed by their enabled_components flag -> (subdomain, label).
_SYNOLOGY = {
    "synology_dsm": ("dsm", "DSM"),
    "synology_photos": ("photos", "Photos"),
    "synology_drive": ("drive", "Drive"),
    "synology_audio": ("audio", "Audio"),
    "synology_video": ("video", "Video"),
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
