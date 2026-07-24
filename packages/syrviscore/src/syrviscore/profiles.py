"""Platform-curated service profiles — coherent sets of catalog services.

A profile turns "assemble a monitoring stack by hand" into one verb:
``syrvis profile enable monitoring`` writes a ``services.d/`` declaration for
each member (resolved from the bundled catalog, so images stay platform-pinned)
and seeds generic default configs into each service's data dir — then a normal
``syrvis reconcile`` converges. Because the declarations are operator-authored
(``services.d:`` authorship), the infra-tier members (node-exporter,
docker-socket-proxy) install with their enumerated read-only host mounts —
which is exactly why the profile path exists: ``service run`` can never grant
that.

Boundaries (the platform/deployment split):

- The platform owns the MEMBER SET and pinned images (catalog templates) and
  the generic defaults (scrape self, null alert routing).
- The deployment owns everything opinionated: real alert receivers, extra
  scrape jobs (e.g. SNMP hardware health with its credentials), dashboards.
  It overrides the seeded configs via ``syrvis deploy`` bundles — profile
  enable NEVER overwrites an existing declaration or config file.

Kept import-light and Python 3.8-clean (runs on the DSM CLI).
"""

from pathlib import Path
from typing import Any, Dict, List

from . import catalog, services_d
from .errors import SyrvisError

# Each config: (packaged source under profile_data/<profile>/, dest under data/).
_MONITORING_CONFIGS = [
    ("vmagent-scrape.yml", "vmagent/config/scrape.yml"),
    ("vmalert-rules-critical.yml", "vmalert/config/rules-critical.yml"),
    ("alertmanager-config.yml", "alertmanager/config/alertmanager.yml"),
]

PROFILES: Dict[str, Dict[str, Any]] = {
    "monitoring": {
        "description": (
            "Observability substrate: victoria-metrics + vmagent + vmalert + "
            "alertmanager, with node-exporter, docker-socket-proxy and "
            "docker-health-exporter as collectors (alert routing stays with "
            "the deployment)"
        ),
        "services": [
            "victoria-metrics",
            "vmagent",
            "vmalert",
            "alertmanager",
            "node-exporter",
            "docker-socket-proxy",
            "docker-health-exporter",
        ],
        "configs": _MONITORING_CONFIGS,
    },
}


class ProfileError(SyrvisError):
    """Unknown profile or a profile member failed to resolve."""

    code = "profile_invalid"


def profile_data_dir(profile: str) -> Path:
    return Path(__file__).parent / "profile_data" / profile


def list_profiles() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "services": list(spec["services"]),
        }
        for name, spec in sorted(PROFILES.items())
    ]


def enable_profile(name: str, home: Path, dry_run: bool = False) -> Dict[str, Any]:
    """Declare a profile's members + seed its default configs (idempotent).

    Never overwrites: an existing declaration or config file is reported and
    left alone — the deployment owns anything it has customized. Members are
    resolved through the catalog's full schema validation, so a bad template
    fails loudly before anything is written.

    Returns a report; converge afterwards with ``syrvis reconcile``.
    """
    spec = PROFILES.get(name)
    if spec is None:
        raise ProfileError(
            "unknown profile {!r} (available: {})".format(name, ", ".join(sorted(PROFILES)))
        )

    declared: List[str] = []
    kept: List[str] = []
    for svc_name in spec["services"]:
        service = catalog.resolve(svc_name)  # full trust boundary; raises CatalogError
        if services_d.declaration_path(home, svc_name).exists():
            kept.append(svc_name)
            continue
        declared.append(svc_name)
        if not dry_run:
            services_d.write_declaration(home, service)

    configs_written: List[str] = []
    configs_kept: List[str] = []
    for src_name, dest_rel in spec.get("configs", []):
        src = profile_data_dir(name) / src_name
        dest = home / "data" / dest_rel
        if dest.exists():
            configs_kept.append(dest_rel)
            continue
        configs_written.append(dest_rel)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text())
            dest.chmod(0o644)

    return {
        "profile": name,
        "dry_run": dry_run,
        "declared": declared,
        "existing_declarations_kept": kept,
        "configs_written": configs_written,
        "configs_kept": configs_kept,
        "next": "syrvis reconcile",
    }
