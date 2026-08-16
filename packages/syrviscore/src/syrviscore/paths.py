"""
Path management for SyrvisCore.

Handles versioned directory structure and provides helpers for common paths.

Directory Structure (v3, split packages):
    /volume1/syrviscore/
    ├── current -> versions/0.1.0/     # Symlink to active version
    ├── versions/
    │   ├── 0.0.1/                     # Previous version (rollback target)
    │   └── 0.1.0/                     # Current active version
    ├── config/                        # Shared configuration
    │   ├── .env
    │   └── docker-compose.yaml
    └── data/                          # Persistent data
"""

import os
import json
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from syrviscore.errors import SyrvisError


class SyrvisHomeError(SyrvisError):
    """Raised when SYRVIS_HOME is not set or invalid."""

    code = "home_not_found"


# Schema version for manifest compatibility
# Must match syrviscore_manager.manifest.MANIFEST_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = 3

# The platform's own directory name under a volume root — `<volume>/syrviscore`.
# Mirrors syrviscore_manager.paths.PACKAGE_NAME. Named here because it is also a
# DSM SHARE-name namespace: a shared folder of this name anywhere on the box
# collides with every plain volume root of the same name at the next cold boot
# (incident 2026-08-16), which is what `check_home_collision` looks for.
PACKAGE_NAME = "syrviscore"


# =============================================================================
# Simulation Mode Support
# =============================================================================


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


def resolve_volume_root(location: str) -> Path:
    """Map a declared ``/volume<N>`` root to its real directory.

    On DSM this is the identity. Under the DSM simulator (and tmp-path tests)
    the volume roots live under ``DSM_SIM_ROOT``, so ``/volume6`` maps to
    ``<sim_root>/volume6`` — which is what lets the v2 app-home layout be
    exercised without a real ``/volumeN``. Kept module-level so tests can
    monkeypatch it directly.
    """
    if is_simulation_mode():
        root = get_sim_root()
        if root is not None:
            return root / str(location).lstrip("/")
    return Path(location)


def is_mounted_volume(location) -> bool:
    """True iff ``location`` is an existing directory that is a real mountpoint.

    The install-time half of the ``location:`` validation (design/26) — the
    schema's parse-time check is regex-only. A SYMLINKED volume root is
    rejected in EVERY mode (explicitly, before any dir test — in prod
    ``os.path.ismount`` would reject it too, but sim mode must not reopen that
    hole, adversarial review #9). Under the DSM simulator only the mountpoint
    requirement itself is waived (sim volumes are plain directories under the
    sim root). Module-level on purpose: tests monkeypatch it.
    """
    target = resolve_volume_root(str(location))
    if os.path.islink(str(target)):
        return False  # a symlinked volume root is never a mounted volume
    if not os.path.isdir(str(target)):
        return False
    if is_simulation_mode():
        return True
    return os.path.ismount(str(target))


def _is_install_manifest(candidate: Path) -> bool:
    """True iff `candidate` holds a parseable install manifest (bare markers fail).

    This is the CONTENT half of the install-root test: the manifest exists,
    parses, and carries install bookkeeping. It deliberately does NOT compare
    `install_path` to the candidate path — a bind-mounted view of a real install
    (the dashboard container mounts the tree at /syrvis) records the HOST path
    and is still a genuine install. Path self-identity is layered on top by
    `_is_install_root` for the SCAN strategies, where a stray copy under a
    location root must never win auto-detection. Any read/parse failure → False
    (fail closed).
    """
    manifest = candidate / ".syrviscore-manifest.json"
    if not candidate.exists() or not manifest.exists():
        return False
    try:
        import json as _json

        data = _json.loads(manifest.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("install_path")) or "schema_version" in data or "versions" in data


def _is_install_root(candidate: Path) -> bool:
    """True iff `candidate` holds a manifest that SELF-IDENTIFIES as this install.

    `_is_install_manifest` plus path identity: a genuine install manifest records
    `install_path` == its own root. A stray copy dropped under a location root
    (e.g. a mis-scoped restore under `/volume6/syrviscore`) records a DIFFERENT
    install_path — so it can never masquerade as the root during auto-detection.
    Manifests that predate `install_path` are accepted on content alone.
    """
    if not _is_install_manifest(candidate):
        return False
    import json as _json

    try:
        data = _json.loads((candidate / ".syrviscore-manifest.json").read_text())
    except Exception:
        return False
    recorded = data.get("install_path")
    if recorded:
        try:
            return Path(recorded).resolve() == candidate.resolve()
        except Exception:
            return False
    return True


def get_syrvis_home() -> Path:
    """
    Get the SYRVIS_HOME directory with auto-detection fallback.

    Tries multiple strategies:
    1. SYRVIS_HOME environment variable
    2. Default location /volume1/syrviscore
    3. Search other volumes (volume2-volume9)
    4. Derive from script location

    Returns:
        Path object for SYRVIS_HOME directory

    Raises:
        SyrvisHomeError: If SYRVIS_HOME cannot be determined
    """
    # Strategy 1: Environment variable.
    #
    # HARDENED (incident 2026-08-16): this used to accept ANY existing directory,
    # with no proof it was an install root — while strategies 2 and 3 below (and
    # the sibling manager package, which requires the manifest for this very
    # variable) do check. That was the one unguarded path AND the one the wrapper
    # exports on every single invocation, so a mis-rooted or renamed home did not
    # fail: it resolved, `load_declarations` returned empty-and-valid for the
    # missing directory, and resume cleared the runstate at the end regardless —
    # a clean, empty, ACTIVE homebase that had converged nothing. Fail LOUD here;
    # an explicit SYRVIS_HOME that is not an install root is always a mistake, and
    # saying so by name is what turns a 50-minute outage into one `ls`.
    syrvis_home = os.environ.get("SYRVIS_HOME")
    if syrvis_home:
        syrvis_path = Path(syrvis_home)
        # Content check only — NOT path self-identity. An explicit SYRVIS_HOME is
        # operator intent, and legitimate bind-mounted views of a real install
        # (the dashboard container mounts the tree at /syrvis) record the HOST
        # path in the manifest. Path identity stays load-bearing for the scan
        # strategies below, where a stray copy must never win auto-detection.
        # (Regression 2026-08-16: the strict check here emptied the dashboard's
        # entire Layer-2 list, silently, on the 0.5.9 image.)
        if _is_install_manifest(syrvis_path):
            return syrvis_path
        raise SyrvisHomeError(
            "SYRVIS_HOME is set to {} but no SyrvisCore installation exists there "
            "(missing or mismatched .syrviscore-manifest.json).\n"
            "The install is far more likely RENAMED than gone — check "
            "`ls -d /volume*/syrviscore*` for a 'syrviscore_1' sibling (DSM renames "
            "a volume root whose name collides with a shared folder at the first cold "
            "boot) and move it back. Do NOT run 'syrvisctl install': that would "
            "create a SECOND tree beside the intact one.".format(syrvis_path)
        )

    # Strategy 2: Default location
    default = Path("/volume1/syrviscore")
    if _is_install_root(default):
        return default

    # Strategy 3: Search other volumes. NB per-service app homes now materialize
    # a real `<location>/syrviscore/apps/` tree on secondary volumes (design/26),
    # so a bare `.syrviscore-manifest.json` marker is no longer sufficient proof
    # of an install root — a stray/mis-scoped copy under a location root would
    # otherwise mis-root the whole CLI. Require the manifest to self-identify.
    for vol_num in range(2, 10):
        candidate = Path(f"/volume{vol_num}/syrviscore")
        if _is_install_root(candidate):
            return candidate

    # Strategy 4: Derive from script location (if installed)
    try:
        script_path = Path(__file__).resolve()
        # Navigate up from src/syrviscore/paths.py to find manifest
        for parent in script_path.parents:
            manifest = parent / ".syrviscore-manifest.json"
            if manifest.exists():
                return parent
    except Exception:
        pass

    raise SyrvisHomeError(
        "Cannot find SyrvisCore installation.\n"
        "Set SYRVIS_HOME environment variable or run from installation directory."
    )


# =============================================================================
# Versioned Directory Structure (v3)
# =============================================================================


def get_versions_dir() -> Path:
    """Get path to versions directory."""
    return get_syrvis_home() / "versions"


def get_current_symlink() -> Path:
    """Get path to 'current' symlink."""
    return get_syrvis_home() / "current"


def get_active_version_dir() -> Path:
    """
    Get path to the active version directory.

    Returns the target of the 'current' symlink, or falls back to
    looking up the active version from manifest.
    """
    current = get_current_symlink()
    if current.exists() and current.is_symlink():
        return current.resolve()

    # Fallback: look up from manifest
    try:
        manifest = get_manifest()
        active = manifest.get("active_version")
        if active:
            return get_versions_dir() / active
    except Exception:
        pass

    raise SyrvisHomeError("No active version found. Run 'syrvis setup' first.")


def get_version_dir(version: str) -> Path:
    """Get path to a specific version directory."""
    return get_versions_dir() / version


def list_installed_versions() -> List[str]:
    """List all installed versions, sorted by semantic version."""
    versions_dir = get_versions_dir()
    if not versions_dir.exists():
        return []

    versions = []
    for item in versions_dir.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            versions.append(item.name)

    # Sort by semantic version (simple approach)
    def version_key(v):
        try:
            parts = v.split(".")
            return tuple(int(p) for p in parts)
        except ValueError:
            return (0, 0, 0)

    return sorted(versions, key=version_key, reverse=True)


def get_active_version() -> Optional[str]:
    """Get the currently active version string."""
    try:
        manifest = get_manifest()
        return manifest.get("active_version")
    except Exception:
        return None


# =============================================================================
# Config Directory (shared across versions)
# =============================================================================


def get_config_dir() -> Path:
    """Get path to shared config directory."""
    return get_syrvis_home() / "config"


def get_env_path() -> Path:
    """Get path to .env configuration file."""
    return get_config_dir() / ".env"


def get_docker_compose_path() -> Path:
    """Get path to docker-compose.yaml file."""
    return get_config_dir() / "docker-compose.yaml"


def get_jobs_dir(syrvis_home: Optional[Path] = None) -> Path:
    """Get path to the operator-writable scheduled-job declarations directory.

    ``config/jobs.d/`` holds one ``<name>.yaml`` job declaration per file (the
    same operator-writable, group-readable treatment as ``config/services.d``).
    A declaration carries {schedule, source, enabled} only — never a command.
    """
    base = Path(syrvis_home) if syrvis_home is not None else get_syrvis_home()
    return base / "config" / "jobs.d"


def get_jobs_script_dir(syrvis_home: Optional[Path] = None) -> Path:
    """Get path to the root-owned directory of vetted job scripts.

    ``<home>/jobs/<name>`` is materialized ``root:root 0755`` by the privileged
    reconcile — the operator can NOT write here. The command a job schedules is
    DERIVED as ``<jobs_dir>/<name>``; it is never declared, so a compromised
    operator cannot schedule an arbitrary command.
    """
    base = Path(syrvis_home) if syrvis_home is not None else get_syrvis_home()
    return base / "jobs"


def get_hooks_dir(syrvis_home: Optional[Path] = None) -> Path:
    """Get path to the root-owned lifecycle hook directory.

    ``<home>/hooks.d/<workload>/<event>`` (and ``hooks.d/instance/<event>``)
    hold ONE executable per transition event. Same trust class as ``jobs/``:
    a hook runs as root only if it AND every parent directory up to here are
    root-owned and not group/world-writable — an unprivileged operator cannot
    author one (derive-not-declare).
    """
    base = Path(syrvis_home) if syrvis_home is not None else get_syrvis_home()
    return base / "hooks.d"


# =============================================================================
# Data Directory (persistent across versions)
# =============================================================================


def get_data_dir() -> Path:
    """Get path to persistent data directory."""
    return get_syrvis_home() / "data"


def get_state_dir(syrvis_home: Optional[Path] = None) -> Path:
    """Get path to the instance state directory (runstate etc.)."""
    base = Path(syrvis_home) if syrvis_home is not None else get_syrvis_home()
    return base / "data" / "state"


def get_runstate_path(syrvis_home: Optional[Path] = None) -> Path:
    """Get path to the persisted instance runstate (absent == active)."""
    return get_state_dir(syrvis_home) / "runstate.json"


def get_traefik_data_dir() -> Path:
    """Get path to Traefik data directory."""
    return get_data_dir() / "traefik"


# =============================================================================
# Version-Specific Paths
# =============================================================================


def get_version_venv_path(version: Optional[str] = None) -> Path:
    """Get path to Python venv for a specific version."""
    if version:
        return get_version_dir(version) / "cli" / "venv"
    return get_active_version_dir() / "cli" / "venv"


def get_version_config_yaml(version: Optional[str] = None) -> Path:
    """Get path to build/config.yaml for a specific version."""
    if version:
        return get_version_dir(version) / "build" / "config.yaml"
    return get_active_version_dir() / "build" / "config.yaml"


def get_config_path() -> Path:
    """
    Get path to build/config.yaml file (active version).

    Legacy compatibility wrapper.
    """
    return get_version_config_yaml()


def get_core_path() -> Path:
    """
    Get path to core data directory.

    Legacy compatibility wrapper.
    """
    return get_data_dir()


# =============================================================================
# Manifest Management
# =============================================================================


def get_manifest_path() -> Path:
    """Get path to installation manifest file."""
    return get_syrvis_home() / ".syrviscore-manifest.json"


def get_manifest() -> Dict[str, Any]:
    """
    Read installation manifest.

    Returns:
        Dictionary containing manifest data

    Raises:
        SyrvisHomeError: If SYRVIS_HOME cannot be determined
        FileNotFoundError: If manifest file doesn't exist
        json.JSONDecodeError: If manifest is invalid JSON
    """
    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    return json.loads(manifest_path.read_text())


def create_manifest(
    version: str,
    install_path: Path,
) -> Dict[str, Any]:
    """
    Create a new manifest with default values.

    Args:
        version: The version being installed
        install_path: Path to SYRVIS_HOME

    Returns:
        New manifest dictionary
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "active_version": version,
        "install_path": str(install_path),
        "setup_complete": False,
        "created_at": datetime.now().isoformat(),
        "versions": {
            version: {
                "installed_at": datetime.now().isoformat(),
                "status": "active",
            }
        },
        "update_history": [],
        "privileged_setup": {},
    }


def update_manifest(updates: Dict[str, Any]) -> None:
    """
    Update manifest file with new values.

    Args:
        updates: Dictionary of values to update in manifest

    Raises:
        SyrvisHomeError: If SYRVIS_HOME cannot be determined
        FileNotFoundError: If manifest file doesn't exist
    """
    manifest_path = get_manifest_path()
    manifest = get_manifest()

    # Deep merge for nested dicts
    def deep_merge(base: dict, update: dict) -> dict:
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    deep_merge(manifest, updates)
    _write_manifest_atomic(manifest_path, manifest)


def save_manifest(manifest: Dict[str, Any]) -> None:
    """
    Save manifest to disk.

    Args:
        manifest: Complete manifest dictionary to save
    """
    _write_manifest_atomic(get_manifest_path(), manifest)


def _write_manifest_atomic(manifest_path: Path, manifest: Dict[str, Any]) -> None:
    """Write the manifest atomically (temp file + rename), 0644.

    Matches syrviscore_manager.manifest.save_manifest so a crash mid-write can
    never leave a truncated manifest. 0644 keeps it world-readable so doctor
    can read it without sudo.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(manifest_path.parent), prefix=".manifest-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, str(manifest_path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def verify_setup_complete() -> bool:
    """
    Check if privileged setup has been completed.

    Returns:
        True if setup is complete, False otherwise
    """
    try:
        manifest = get_manifest()
        return manifest.get("setup_complete", False)
    except Exception:
        return False


def add_version_to_manifest(version: str, status: str = "available") -> None:
    """
    Add a new version entry to the manifest.

    Args:
        version: Version string (e.g., "0.1.0")
        status: Version status ("available", "active", "deprecated")
    """
    update_manifest(
        {
            "versions": {
                version: {
                    "installed_at": datetime.now().isoformat(),
                    "status": status,
                }
            }
        }
    )


def set_active_version(version: str) -> None:
    """
    Set a version as active in the manifest.

    Args:
        version: Version string to activate
    """
    manifest = get_manifest()

    # Update previous active version status
    old_version = manifest.get("active_version")
    if old_version and old_version in manifest.get("versions", {}):
        manifest["versions"][old_version]["status"] = "available"

    # Set new active version
    manifest["active_version"] = version
    if version in manifest.get("versions", {}):
        manifest["versions"][version]["status"] = "active"
        manifest["versions"][version]["activated_at"] = datetime.now().isoformat()

    # Add to update history
    if old_version and old_version != version:
        history_entry = {
            "from": old_version,
            "to": version,
            "timestamp": datetime.now().isoformat(),
            "type": "upgrade" if version > old_version else "rollback",
        }
        if "update_history" not in manifest:
            manifest["update_history"] = []
        manifest["update_history"].append(history_entry)

    save_manifest(manifest)


# =============================================================================
# Directory Creation Helpers
# =============================================================================


def ensure_directory_structure(install_path: Path, version: str) -> None:
    """
    Create the complete directory structure for a new installation.

    Args:
        install_path: Path to SYRVIS_HOME
        version: Version being installed
    """
    # Root directories
    (install_path / "versions").mkdir(parents=True, exist_ok=True)
    (install_path / "config").mkdir(exist_ok=True)
    (install_path / "config" / "traefik").mkdir(exist_ok=True)
    (install_path / "data").mkdir(exist_ok=True)
    (install_path / "data" / "traefik").mkdir(exist_ok=True)
    (install_path / "data" / "traefik" / "config").mkdir(exist_ok=True)
    (install_path / "data" / "traefik" / "logs").mkdir(exist_ok=True)
    (install_path / "data" / "portainer").mkdir(exist_ok=True)
    (install_path / "data" / "cloudflared").mkdir(exist_ok=True)

    # Version-specific directories
    version_dir = install_path / "versions" / version
    version_dir.mkdir(exist_ok=True)
    (version_dir / "cli").mkdir(exist_ok=True)
    (version_dir / "build").mkdir(exist_ok=True)


def update_current_symlink(version: str) -> None:
    """
    Update the 'current' symlink to point to a version.

    Args:
        version: Version to point to
    """
    syrvis_home = get_syrvis_home()
    current = syrvis_home / "current"
    target = Path("versions") / version  # Relative path

    # Remove existing symlink if present
    if current.exists() or current.is_symlink():
        current.unlink()

    # Create new symlink
    current.symlink_to(target)


# =============================================================================
# Validation Helpers
# =============================================================================


def validate_docker_compose_exists() -> None:
    """
    Validate that docker-compose.yaml exists.

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
        FileNotFoundError: If docker-compose.yaml doesn't exist
    """
    compose_path = get_docker_compose_path()

    if not compose_path.exists():
        raise FileNotFoundError(
            f"docker-compose.yaml not found at {compose_path}\n"
            "Run 'syrvis setup' to complete installation."
        )


# =============================================================================
# Testing Helpers
# =============================================================================


def set_syrvis_home(path: str) -> None:
    """
    Set SYRVIS_HOME environment variable (for testing).

    Args:
        path: Path to set as SYRVIS_HOME
    """
    os.environ["SYRVIS_HOME"] = path


def unset_syrvis_home() -> None:
    """
    Unset SYRVIS_HOME environment variable (for testing).
    """
    if "SYRVIS_HOME" in os.environ:
        del os.environ["SYRVIS_HOME"]
