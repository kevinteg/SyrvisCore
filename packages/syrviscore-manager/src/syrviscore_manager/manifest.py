"""
Manifest management for SyrvisCore Manager.

The manifest tracks:
- Installed versions and their status
- Active version
- Setup completion status
- Update history
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from .paths import get_manifest_path, get_syrvis_home, SyrvisHomeError
from .downloader import compare_versions


# Schema version for manifest compatibility
MANIFEST_SCHEMA_VERSION = 3  # v3 for split packages


def create_manifest(install_path: Path) -> Dict[str, Any]:
    """
    Create a new manifest with default values.

    Args:
        install_path: Path to SYRVIS_HOME

    Returns:
        New manifest dictionary
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "active_version": None,
        "install_path": str(install_path),
        "setup_complete": False,
        "created_at": datetime.now().isoformat(),
        "versions": {},
        "update_history": [],
        "privileged_setup": {},
    }


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


def ensure_manifest() -> Dict[str, Any]:
    """
    Get or create the manifest file.

    Returns:
        Manifest dictionary
    """
    try:
        return get_manifest()
    except FileNotFoundError:
        syrvis_home = get_syrvis_home()
        manifest = create_manifest(syrvis_home)
        save_manifest(manifest)
        return manifest


def get_active_version() -> Optional[str]:
    """Get the currently active version string."""
    try:
        manifest = get_manifest()
        return manifest.get("active_version")
    except (SyrvisHomeError, FileNotFoundError):
        return None


def add_version_to_manifest(version: str, status: str = "available") -> None:
    """
    Add a new version entry to the manifest.

    Args:
        version: Version string (e.g., "0.1.0")
        status: Version status ("available", "active", "deprecated")
    """
    manifest = ensure_manifest()

    if "versions" not in manifest:
        manifest["versions"] = {}

    manifest["versions"][version] = {
        "installed_at": datetime.now().isoformat(),
        "status": status,
    }

    save_manifest(manifest)


def remove_version_from_manifest(version: str) -> None:
    """
    Remove a version entry from the manifest.

    Args:
        version: Version string to remove
    """
    try:
        manifest = get_manifest()
        if version in manifest.get("versions", {}):
            del manifest["versions"][version]
            save_manifest(manifest)
    except (SyrvisHomeError, FileNotFoundError):
        pass


def set_active_version(version: str) -> None:
    """
    Set a version as active in the manifest.

    Args:
        version: Version string to activate
    """
    manifest = ensure_manifest()

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
        # Use proper version comparison (handles 0.10.0 vs 0.2.0 correctly)
        is_upgrade = compare_versions(version, old_version) > 0
        history_entry = {
            "from": old_version,
            "to": version,
            "timestamp": datetime.now().isoformat(),
            "type": "upgrade" if is_upgrade else "rollback",
        }
        if "update_history" not in manifest:
            manifest["update_history"] = []
        manifest["update_history"].append(history_entry)

    save_manifest(manifest)


def verify_setup_complete() -> bool:
    """
    Check if privileged setup has been completed.

    Returns:
        True if setup is complete, False otherwise
    """
    try:
        manifest = get_manifest()
        return manifest.get('setup_complete', False)
    except (SyrvisHomeError, FileNotFoundError):
        return False


def mark_setup_complete() -> None:
    """Mark setup as complete in manifest."""
    manifest = ensure_manifest()
    manifest["setup_complete"] = True
    save_manifest(manifest)


def get_version_info(version: str) -> Optional[Dict[str, Any]]:
    """
    Get info about a specific version.

    Args:
        version: Version string

    Returns:
        Version info dictionary or None
    """
    try:
        manifest = get_manifest()
        return manifest.get("versions", {}).get(version)
    except (SyrvisHomeError, FileNotFoundError):
        return None


def get_update_history() -> list:
    """Get the update history from manifest."""
    try:
        manifest = get_manifest()
        return manifest.get("update_history", [])
    except (SyrvisHomeError, FileNotFoundError):
        return []
