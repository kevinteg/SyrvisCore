"""File-plane shares registry — the resolver behind ``fileplane=`` volumes.

A *share declaration* is a YAML file under ``$SYRVIS_HOME/shares.d/`` describing
a NAS file share that exists OUTSIDE the SyrvisCore data tree (a family media
share, a documents share — bytes whose lifecycle the operator owns, not the
platform). SyrvisCore consumes a deliberately MINIMAL contract from each file
and ignores every other key, so a deployment repo may keep richer semantics
(ACLs, backup scopes, sensitivity classes) in the same files without coupling
the engine to them:

    id: gaming            # optional — defaults to the filename stem
    share_name: Gaming    # the DSM share name (path component)
    volume: /volume5      # the DSM volume root
    class: resting        # optional — 'resting' shares refuse rw binds unless…
    writers: [syncthing]  # optional — services sanctioned for rw binds

Resolution: ``share 'gaming' -> /volume5/Gaming``. The registry exists so a
service declaration can reference a share BY ID and never by path — absolute
host paths stay refused everywhere (service_schema mount policy), and the
engine renders the real bind only for shares the operator has declared.

Registry absent or share undeclared -> a typed error at deploy/render time,
never a silent fallback: an unbindable share is the feature working.
"""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List

import yaml

from syrviscore.errors import SyrvisError


class SharesRegistryError(SyrvisError, ValueError):
    """A share declaration is missing, malformed, or refuses the request."""


@dataclass
class ShareDeclaration:
    """The engine-consumed slice of one shares.d declaration."""

    share_id: str
    root: str  # absolute host path: <volume>/<share_name>
    share_class: str = ""  # optional; 'resting' gates rw binds
    writers: List[str] = field(default_factory=list)


def _load_one(path: Path) -> ShareDeclaration:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise SharesRegistryError("Share declaration {} is not valid YAML: {}".format(path, exc))
    if not isinstance(data, dict):
        raise SharesRegistryError("Share declaration {} must be a YAML mapping".format(path))

    share_id = str(data.get("id") or path.stem)
    share_name = data.get("share_name")
    volume = data.get("volume")
    if not share_name or not isinstance(share_name, str):
        raise SharesRegistryError(
            "Share declaration {}: 'share_name' is required (the DSM share name)".format(path)
        )
    if not volume or not isinstance(volume, str) or not PurePosixPath(volume).is_absolute():
        raise SharesRegistryError(
            "Share declaration {}: 'volume' is required and must be an absolute "
            "path (e.g. /volume5)".format(path)
        )
    if "/" in share_name or ".." in (share_name, volume) or ".." in PurePosixPath(volume).parts:
        raise SharesRegistryError(
            "Share declaration {}: share_name/volume may not contain path traversal".format(path)
        )

    writers = data.get("writers") or []
    if not isinstance(writers, list) or not all(isinstance(w, str) for w in writers):
        raise SharesRegistryError(
            "Share declaration {}: 'writers' must be a list of service names".format(path)
        )

    return ShareDeclaration(
        share_id=share_id,
        root=str(PurePosixPath(volume) / share_name),
        share_class=str(data.get("class") or ""),
        writers=list(writers),
    )


def load_shares(syrvis_home: Path) -> Dict[str, ShareDeclaration]:
    """Load every declaration under ``$SYRVIS_HOME/shares.d/`` (may be empty).

    Malformed declarations raise rather than being skipped — a registry that
    silently drops entries would turn a typo into a phantom 'share not
    declared' error somewhere else.
    """
    shares_dir = Path(syrvis_home) / "shares.d"
    registry: Dict[str, ShareDeclaration] = {}
    if not shares_dir.is_dir():
        return registry
    for path in sorted(shares_dir.glob("*.yaml")):
        decl = _load_one(path)
        if decl.share_id in registry:
            raise SharesRegistryError(
                "Duplicate share id {!r} (second declaration: {})".format(decl.share_id, path)
            )
        registry[decl.share_id] = decl
    return registry


def resolve_fileplane(
    registry: Dict[str, ShareDeclaration],
    service_name: str,
    share_id: str,
    subpath: str,
    mode: str,
) -> str:
    """Resolve a validated fileplane reference to an absolute host path.

    Policy enforced here (render time — the schema already validated shape):
      * the share must be declared;
      * ``rw`` against a ``class: resting`` share requires the service to be
        named in the share's ``writers:`` list (the ingest-contract check).
    """
    decl = registry.get(share_id)
    if decl is None:
        raise SharesRegistryError(
            "Volume references undeclared share {!r}: no shares.d/{}.yaml under "
            "SYRVIS_HOME (declare the share first — fileplane binds are only "
            "expressible against the registry)".format(share_id, share_id)
        )
    if mode == "rw" and decl.share_class == "resting" and service_name not in decl.writers:
        raise SharesRegistryError(
            "Service {!r} requests rw on resting share {!r} but is not in its "
            "writers: list — file-plane rw is sanctioned by the share "
            "declaration, not the service".format(service_name, share_id)
        )
    host = PurePosixPath(decl.root)
    if subpath:
        host = host / subpath
    return str(host)
