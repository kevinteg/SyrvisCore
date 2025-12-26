"""
Path management for SyrvisCore.

Handles SYRVIS_HOME environment variable and provides helpers for common paths.
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any


class SyrvisHomeError(Exception):
    """Raised when SYRVIS_HOME is not set or invalid."""

    pass


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
    except:
        pass

    raise SyrvisHomeError(
        "Cannot find SyrvisCore installation.\n"
        "Set SYRVIS_HOME environment variable or run from installation directory."
    )


def get_docker_compose_path() -> Path:
    """
    Get path to docker-compose.yaml file.

    Returns:
        Path to docker-compose.yaml in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "docker-compose.yaml"


def get_config_path() -> Path:
    """
    Get path to build/config.yaml file.

    Returns:
        Path to build/config.yaml in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "build" / "config.yaml"


def get_core_path() -> Path:
    """
    Get path to core data directory.

    Returns:
        Path to data directory in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "data"


def validate_docker_compose_exists() -> None:
    """
    Validate that docker-compose.yaml exists in SYRVIS_HOME.

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
        FileNotFoundError: If docker-compose.yaml doesn't exist
    """
    compose_path = get_docker_compose_path()

    if not compose_path.exists():
        raise FileNotFoundError(
            f"docker-compose.yaml not found in SYRVIS_HOME ({get_syrvis_home()})\n"
            "Run 'syrvis generate-compose' to create it."
        )


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
    manifest.update(updates)
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


def get_env_path() -> Path:
    """Get path to .env configuration file."""
    return get_syrvis_home() / ".env"


def get_env_template_path() -> Path:
    """Get path to .env.template file."""
    return get_syrvis_home() / ".env.template"
