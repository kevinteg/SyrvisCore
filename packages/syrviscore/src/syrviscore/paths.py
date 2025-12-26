"""
Path management for SyrvisCore service.

Handles versioned directory structure and provides helpers for common paths.

Directory Structure:
    /volumeX/docker/syrviscore/
    ├── current -> versions/0.1.0/     # Symlink to active version
    ├── versions/
    │   ├── 0.0.1/                     # Previous version
    │   └── 0.1.0/                     # Current active version
    ├── config/                        # Shared configuration
    │   ├── .env
    │   └── docker-compose.yaml
    └── data/                          # Persistent data
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from .__version__ import __version__


class SyrvisHomeError(Exception):
    """Raised when SYRVIS_HOME is not set or invalid."""
    pass


# Schema version for manifest compatibility
MANIFEST_SCHEMA_VERSION = 3


def get_syrvis_home() -> Path:
    """
    Get the SYRVIS_HOME directory with auto-detection fallback.

    Tries multiple strategies:
    1. SYRVIS_HOME environment variable
    2. Default location /volume1/docker/syrviscore
    3. Search other volumes (volume2-volume9)
    4. Derive from script location

    Returns:
        Path object for SYRVIS_HOME directory

    Raises:
        SyrvisHomeError: If SYRVIS_HOME cannot be determined
    """
    # Strategy 1: Environment variable
    syrvis_home = os.environ.get("SYRVIS_HOME")
    if syrvis_home:
        syrvis_path = Path(syrvis_home)
        if syrvis_path.exists() and syrvis_path.is_dir():
            return syrvis_path

    # Strategy 2: Default location
    default = Path("/volume1/docker/syrviscore")
    if default.exists() and (default / ".syrviscore-manifest.json").exists():
        return default

    # Strategy 3: Search other volumes
    for vol_num in range(2, 10):
        candidate = Path(f"/volume{vol_num}/docker/syrviscore")
        if candidate.exists() and (candidate / ".syrviscore-manifest.json").exists():
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
# Versioned Directory Structure
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
        if item.is_dir() and not item.name.startswith('.'):
            versions.append(item.name)

    # Sort by semantic version (simple approach)
    def version_key(v):
        try:
            parts = v.split('.')
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


def get_env_template_path() -> Path:
    """Get path to .env.template file."""
    return get_config_dir() / ".env.template"


def get_docker_compose_path() -> Path:
    """Get path to docker-compose.yaml file."""
    return get_config_dir() / "docker-compose.yaml"


def get_traefik_config_dir() -> Path:
    """Get path to Traefik config directory."""
    return get_config_dir() / "traefik"


# =============================================================================
# Data Directory (persistent across versions)
# =============================================================================

def get_data_dir() -> Path:
    """Get path to persistent data directory."""
    return get_syrvis_home() / "data"


def get_traefik_data_dir() -> Path:
    """Get path to Traefik data directory."""
    return get_data_dir() / "traefik"


def get_portainer_data_dir() -> Path:
    """Get path to Portainer data directory."""
    return get_data_dir() / "portainer"


def get_cloudflared_data_dir() -> Path:
    """Get path to Cloudflared data directory."""
    return get_data_dir() / "cloudflared"


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
    """
    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    return json.loads(manifest_path.read_text())


def save_manifest(manifest: Dict[str, Any]) -> None:
    """
    Save manifest to disk.

    Args:
        manifest: Complete manifest dictionary to save
    """
    manifest_path = get_manifest_path()
    manifest_path.write_text(json.dumps(manifest, indent=2))


def update_manifest(updates: Dict[str, Any]) -> None:
    """
    Update manifest file with new values.

    Args:
        updates: Dictionary of values to update in manifest
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
    manifest_path.write_text(json.dumps(manifest, indent=2))


def verify_setup_complete() -> bool:
    """
    Check if privileged setup has been completed.

    Returns:
        True if setup is complete, False otherwise
    """
    try:
        manifest = get_manifest()
        return manifest.get('setup_complete', False)
    except Exception:
        return False


def mark_setup_complete() -> None:
    """Mark setup as complete in manifest."""
    update_manifest({"setup_complete": True})


# =============================================================================
# Directory Creation Helpers
# =============================================================================

def ensure_config_directories() -> None:
    """Ensure config directories exist."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "traefik").mkdir(exist_ok=True)


def ensure_data_directories() -> None:
    """Ensure data directories exist."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "traefik").mkdir(exist_ok=True)
    (data_dir / "traefik" / "config").mkdir(exist_ok=True)
    (data_dir / "portainer").mkdir(exist_ok=True)
    (data_dir / "cloudflared").mkdir(exist_ok=True)


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
