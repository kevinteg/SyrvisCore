"""
Path management for SyrvisCore Manager.

Handles discovery of SYRVIS_HOME and SPK installation directories, version
directory containment, and the atomic ``current`` symlink switch.

Design rules (v2):
- The home directory is resolved once at the CLI boundary (``resolve_home``)
  and passed explicitly to every function — no ambient ``os.environ`` mutation.
- Version strings are validated (strict MAJOR.MINOR.PATCH) before they are
  ever used as path components.
- The ``current`` symlink is the single source of truth for the active
  version; switching it is atomic (tmp symlink + os.replace).

Directory Structure:
    /var/packages/syrviscore/target/      # SPK install (manager venv)

    /volumeX/syrviscore/                  # SYRVIS_HOME
    ├── current -> versions/0.1.0/         # Symlink to active version
    ├── versions/
    │   ├── 0.0.1/cli/venv/bin/syrvis      # Previous version
    │   └── 0.1.0/cli/venv/bin/syrvis      # Active version
    ├── config/                            # Shared config
    ├── data/                              # Persistent data
    └── .syrviscore-manifest.json
"""

import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from .errors import AmbiguousHomeError, HomeNotFoundError, InvalidVersionError

# Backwards-compatible alias (pre-v2 code and scripts referenced this name)
SyrvisHomeError = HomeNotFoundError

# Default package name
PACKAGE_NAME = "syrviscore"

# SPK installation directory
SPK_TARGET_DIR = "/var/packages/{}/target".format(PACKAGE_NAME)

MANIFEST_FILENAME = ".syrviscore-manifest.json"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

_VOLUME_RE = re.compile(r"^volume\d+$")


def validate_version(version: str) -> str:
    """Validate a version string (strict MAJOR.MINOR.PATCH).

    Accepts a single leading 'v' prefix and strips it.

    Raises:
        InvalidVersionError: If the version is not N.N.N
    """
    if not isinstance(version, str) or not version:
        raise InvalidVersionError("Version must be a non-empty string")
    v = version[1:] if version.startswith("v") else version
    if not VERSION_RE.match(v):
        raise InvalidVersionError(
            "Invalid version {!r}: expected MAJOR.MINOR.PATCH (e.g. 0.2.0)".format(version)
        )
    return v


def get_package_volume() -> Optional[str]:
    """
    Detect the volume where the SPK package is installed.

    Tries multiple strategies:
    1. SYNOPKG_PKGDEST environment variable (set during SPK installation)
    2. Location of the syrvisctl executable itself
    3. Location of this module

    Returns:
        Volume path (e.g., "/volume1") or None if not detectable
    """

    def _volume_of(path_str: str) -> Optional[str]:
        parts = path_str.split("/")
        if len(parts) >= 2 and _VOLUME_RE.match(parts[1]):
            return "/" + parts[1]
        return None

    pkg_dest = os.environ.get("SYNOPKG_PKGDEST", "")
    if pkg_dest:
        vol = _volume_of(pkg_dest)
        if vol:
            return vol

    if sys.executable:
        vol = _volume_of(sys.executable)
        if vol:
            return vol

    return _volume_of(str(Path(__file__).resolve()))


def is_simulation_mode() -> bool:
    """Check if running in DSM simulation mode."""
    return os.environ.get("DSM_SIM_ACTIVE") == "1"


def get_sim_root() -> Optional[Path]:
    """Get simulation root path if in simulation mode."""
    if is_simulation_mode():
        sim_root = os.environ.get("DSM_SIM_ROOT")
        if sim_root:
            return Path(sim_root)
    return None


def get_spk_target_dir() -> Path:
    """Get the SPK installation target directory."""
    sim_root = get_sim_root()
    if sim_root:
        return sim_root / "var" / "packages" / PACKAGE_NAME / "target"
    return Path(SPK_TARGET_DIR)


def _volumes_root() -> Path:
    sim_root = get_sim_root()
    return sim_root if sim_root else Path("/")


def _candidate_homes() -> List[Path]:
    """All existing installations found by scanning volumes (manifest present)."""
    root = _volumes_root()
    candidates = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not _VOLUME_RE.match(entry.name):
            continue
        candidate = entry / PACKAGE_NAME
        if (candidate / MANIFEST_FILENAME).exists():
            candidates.append(candidate)
    return candidates


def is_installation(path: Path) -> bool:
    """True if the path looks like an existing SyrvisCore installation."""
    return (path / MANIFEST_FILENAME).exists()


def resolve_home(explicit: Optional[Path] = None, create: bool = False) -> Path:
    """
    Resolve the SYRVIS_HOME directory.

    Resolution order:
    1. ``explicit`` (e.g. from a --path option)
    2. ``SYRVIS_HOME`` environment variable
    3. The package volume's installation (``/volumeX/syrviscore`` with manifest)
    4. Scan all volumes for exactly one existing installation

    Args:
        explicit: Explicit path from the caller (highest priority).
        create: If True, an explicit/env path is created when missing instead
            of being required to be an existing installation, and a default
            path is created when nothing else resolves.

    Raises:
        HomeNotFoundError: No installation found (and create is False).
        AmbiguousHomeError: Multiple installations found by the volume scan.
    """
    for source, raw in (("--path", explicit), ("SYRVIS_HOME", os.environ.get("SYRVIS_HOME"))):
        if not raw:
            continue
        path = Path(raw)
        if is_installation(path):
            return path
        if create:
            path.mkdir(parents=True, exist_ok=True)
            return path
        raise HomeNotFoundError(
            "{} is set to {} but no SyrvisCore installation exists there "
            "(missing {}).".format(source, path, MANIFEST_FILENAME)
        )

    pkg_volume = get_package_volume()
    if pkg_volume:
        sim_root = get_sim_root()
        if sim_root:
            candidate = sim_root / pkg_volume.lstrip("/") / PACKAGE_NAME
        else:
            candidate = Path(pkg_volume) / PACKAGE_NAME
        if is_installation(candidate):
            return candidate

    candidates = _candidate_homes()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise AmbiguousHomeError(
            "Multiple SyrvisCore installations found: {}. "
            "Set SYRVIS_HOME or pass --path to choose one.".format(
                ", ".join(str(c) for c in candidates)
            )
        )

    if create:
        home = get_default_install_path()
        home.mkdir(parents=True, exist_ok=True)
        return home

    raise HomeNotFoundError(
        "Cannot find SyrvisCore installation.\n"
        "Run 'syrvisctl install' to install a service version."
    )


def get_default_install_path() -> Path:
    """
    Get the default installation path for new installs.

    Uses the package volume if available, otherwise /volume1.
    """
    sim_root = get_sim_root()
    pkg_volume = get_package_volume()

    base = Path(pkg_volume) if pkg_volume else Path("/volume1")

    if sim_root:
        return sim_root / base.relative_to("/") / PACKAGE_NAME
    return base / PACKAGE_NAME


# =============================================================================
# Home-scoped paths (all take the resolved home explicitly)
# =============================================================================


def versions_dir(home: Path) -> Path:
    """Get path to versions directory."""
    return home / "versions"


def version_dir(home: Path, version: str) -> Path:
    """Get path to a specific version directory (version is validated)."""
    v = validate_version(version)
    target = versions_dir(home) / v
    if target.parent != versions_dir(home):
        raise InvalidVersionError("Version {!r} escapes the versions directory".format(version))
    return target


def current_symlink(home: Path) -> Path:
    """Get path to 'current' symlink."""
    return home / "current"


def manifest_path(home: Path) -> Path:
    """Get path to installation manifest file."""
    return home / MANIFEST_FILENAME


def get_syrvis_profile_path(home: Path) -> Path:
    """Get the path to the syrvis profile snippet."""
    return home / "syrvis.profile"


def active_version(home: Path) -> Optional[str]:
    """Get the active version from the ``current`` symlink (source of truth)."""
    current = current_symlink(home)
    if not current.is_symlink():
        return None
    target = os.readlink(str(current))
    name = Path(target).name
    try:
        return validate_version(name)
    except InvalidVersionError:
        return None


def list_installed_versions(home: Path) -> List[str]:
    """List all installed versions, sorted by semantic version (newest first)."""
    vdir = versions_dir(home)
    if not vdir.exists():
        return []

    versions = []
    for item in vdir.iterdir():
        if not item.is_dir() or item.name.startswith("."):
            continue
        if not VERSION_RE.match(item.name):
            continue
        # Verify it has a venv (properly installed)
        if (item / "cli" / "venv").exists():
            versions.append(item.name)

    return sorted(versions, key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)


def ensure_directory_structure(home: Path) -> None:
    """Create the shared directory structure for an installation."""
    (home / "versions").mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "traefik").mkdir(exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    (home / "data" / "traefik").mkdir(exist_ok=True)
    (home / "data" / "traefik" / "config").mkdir(exist_ok=True)
    (home / "data" / "portainer").mkdir(exist_ok=True)
    (home / "data" / "cloudflared").mkdir(exist_ok=True)
    (home / "bin").mkdir(exist_ok=True)


def update_current_symlink(home: Path, version: str) -> None:
    """
    Atomically point the ``current`` symlink at a version.

    Uses a temporary symlink + os.replace so there is no window in which
    ``current`` is missing, and concurrent switches cannot corrupt it.
    """
    v = validate_version(version)
    current = current_symlink(home)
    target = Path("versions") / v  # Relative path

    if current.exists() and not current.is_symlink():
        raise HomeNotFoundError(
            "{} exists but is not a symlink; refusing to replace it. "
            "Move it aside and re-run.".format(current)
        )

    tmp = home / ".current.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(str(tmp), str(current))


def create_syrvis_wrapper(home: Path) -> Path:
    """Create the syrvis wrapper script in bin/."""
    bin_dir = home / "bin"
    bin_dir.mkdir(exist_ok=True)

    wrapper_path = bin_dir / "syrvis"
    wrapper_content = """#!/bin/sh
# SyrvisCore CLI Wrapper
# Auto-generated by syrvisctl

INSTALL_DIR="{home}"
export SYRVIS_HOME="${{INSTALL_DIR}}"

CURRENT_VERSION="${{INSTALL_DIR}}/current"
if [ -L "$CURRENT_VERSION" ]; then
    exec "${{CURRENT_VERSION}}/cli/venv/bin/syrvis" "$@"
else
    echo "Error: No service version installed."
    echo "Run 'syrvisctl install' to install a service version."
    exit 1
fi
""".format(
        home=home
    )
    wrapper_path.write_text(wrapper_content)
    wrapper_path.chmod(0o755)
    return wrapper_path


def create_syrvis_profile(home: Path) -> Path:
    """Create a profile snippet for the syrvis CLI."""
    bin_dir = home / "bin"

    profile_path = get_syrvis_profile_path(home)
    profile_content = """# SyrvisCore Service CLI PATH configuration
# Source this file to add syrvis to your PATH:
#   source {profile}
export SYRVIS_HOME="{home}"
export PATH="${{PATH}}:{bin_dir}"
""".format(
        profile=profile_path, home=home, bin_dir=bin_dir
    )
    profile_path.write_text(profile_content)
    profile_path.chmod(0o644)
    return profile_path
