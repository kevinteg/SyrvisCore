"""A file-based catalog of vetted Layer 2 service templates.

Makes the common case one word: ``syrvis service run gollum`` (no ``--image``)
resolves ``gollum`` from the catalog, applies any ``--subdomain/--exposure/
--port/--env`` overrides, and installs it through the exact same trust boundary
as every other service.

Two template sources, later wins on name collision:
1. **Bundled** — ``syrviscore/catalog_templates/*.yaml`` shipped in the wheel:
   a small set of known-good, version-pinned definitions.
2. **Site-local** — ``$SYRVIS_HOME/catalog/*.yaml``: operator-added templates
   (also lets an operator pin a different version of a bundled one).

Templates are ordinary ``syrvis-service.yaml`` documents and are validated by
:class:`~syrviscore.service_schema.ServiceDefinition` at resolve time, so a bad
or tampered template fails loudly before anything is installed. The catalog
ships TEMPLATES, not site config: exposure/subdomain choices stay with the
operator (or their overrides).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .errors import SyrvisError
from .service_schema import ServiceDefinition


class CatalogError(SyrvisError):
    """A catalog template is missing or invalid."""

    code = "catalog_invalid"


def bundled_dir() -> Path:
    """The templates directory shipped inside the package."""
    return Path(__file__).parent / "catalog_templates"


def site_dir() -> Optional[Path]:
    """The operator's site-local catalog dir, when a home is resolvable."""
    try:
        from . import paths

        return paths.get_syrvis_home() / "catalog"
    except Exception:  # noqa: BLE001 - no install context (tests, fresh box)
        return None


def _template_files() -> Dict[str, Path]:
    """{name: path} across both sources; site-local overrides bundled."""
    files: Dict[str, Path] = {}
    for directory in (bundled_dir(), site_dir()):
        if directory is None or not directory.exists():
            continue
        for path in sorted(directory.glob("*.yaml")):
            files[path.stem] = path
    return files


def list_templates() -> List[Dict[str, Any]]:
    """Summaries of every resolvable template (invalid ones are reported, not hidden)."""
    entries = []
    for name, path in sorted(_template_files().items()):
        try:
            svc = resolve(name)
            entries.append(
                {
                    "name": name,
                    "image": svc.image,
                    "description": svc.description,
                    "subdomain": svc.traefik.subdomain if svc.traefik.enabled else "",
                    "exposure": svc.traefik.exposure if svc.traefik.enabled else None,
                    "source": "site" if site_dir() and path.parent == site_dir() else "bundled",
                }
            )
        except CatalogError as exc:
            entries.append({"name": name, "error": str(exc)})
    return entries


def resolve(name: str) -> ServiceDefinition:
    """Load + validate the template ``name`` into a ServiceDefinition.

    Raises:
        CatalogError: unknown template, unparseable YAML, a definition that
            fails the service schema, or a template whose ``name`` field does
            not match its filename (prevents a template silently installing
            under a different identity than the one requested).
    """
    files = _template_files()
    path = files.get(name)
    if path is None:
        known = ", ".join(sorted(files)) or "(none)"
        raise CatalogError(
            "No catalog template named {!r}. Available: {}. "
            "(Or pass --image to run an arbitrary pinned image.)".format(name, known)
        )
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise CatalogError("Template {} is not valid YAML: {}".format(path.name, exc))
    if not isinstance(data, dict):
        raise CatalogError("Template {} must be a mapping".format(path.name))
    try:
        svc = ServiceDefinition.from_dict(data)
    except Exception as exc:  # noqa: BLE001 - surface schema violations uniformly
        raise CatalogError("Template {} failed validation: {}".format(path.name, exc))
    if svc.name != name:
        raise CatalogError(
            "Template {} declares name {!r} — it must match its filename".format(
                path.name, svc.name
            )
        )
    return svc
