"""
Manifest management for SyrvisCore Manager.

The manifest tracks installation metadata:
- Installed versions and their status
- Setup completion status
- Update history

The ``current`` symlink — not the manifest — is the source of truth for the
active version (see paths.active_version). The manifest mirrors it for
convenience and history, and is reconciled on every write.

All writes are atomic (temp file + os.replace) so a crash can never leave a
corrupt manifest behind.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths
from .downloader import compare_versions

# Schema version for manifest compatibility
MANIFEST_SCHEMA_VERSION = 3  # v3 for split packages


def create_manifest(home: Path) -> Dict[str, Any]:
    """Create a new manifest with default values."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "active_version": None,
        "install_path": str(home),
        "setup_complete": False,
        "created_at": datetime.now().isoformat(),
        "versions": {},
        "update_history": [],
        "privileged_setup": {},
    }


def get_manifest(home: Path) -> Dict[str, Any]:
    """
    Read the installation manifest.

    Raises:
        FileNotFoundError: If manifest file doesn't exist
    """
    mpath = paths.manifest_path(home)
    if not mpath.exists():
        raise FileNotFoundError("Manifest not found: {}".format(mpath))
    return json.loads(mpath.read_text())


def save_manifest(home: Path, manifest: Dict[str, Any]) -> None:
    """Save manifest to disk atomically (temp file + rename)."""
    mpath = paths.manifest_path(home)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(mpath.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, str(mpath))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_manifest(home: Path) -> Dict[str, Any]:
    """Get or create the manifest file."""
    try:
        return get_manifest(home)
    except FileNotFoundError:
        manifest = create_manifest(home)
        save_manifest(home, manifest)
        return manifest


def get_active_version(home: Path) -> Optional[str]:
    """
    Get the currently active version.

    The ``current`` symlink is authoritative; the manifest is only consulted
    when no symlink exists (e.g. partially restored installations).
    """
    from_symlink = paths.active_version(home)
    if from_symlink:
        return from_symlink
    try:
        return get_manifest(home).get("active_version")
    except FileNotFoundError:
        return None


def add_version_to_manifest(home: Path, version: str, status: str = "available") -> None:
    """Add a new version entry to the manifest."""
    manifest = ensure_manifest(home)
    manifest.setdefault("versions", {})[version] = {
        "installed_at": datetime.now().isoformat(),
        "status": status,
    }
    save_manifest(home, manifest)


def remove_version_from_manifest(home: Path, version: str) -> None:
    """Remove a version entry from the manifest."""
    try:
        manifest = get_manifest(home)
    except FileNotFoundError:
        return
    if version in manifest.get("versions", {}):
        del manifest["versions"][version]
        save_manifest(home, manifest)


def set_active_version(home: Path, version: str) -> None:
    """Record a version as active in the manifest (mirrors the symlink)."""
    manifest = ensure_manifest(home)

    old_version = manifest.get("active_version")
    if old_version and old_version in manifest.get("versions", {}):
        manifest["versions"][old_version]["status"] = "available"

    manifest["active_version"] = version
    if version in manifest.get("versions", {}):
        manifest["versions"][version]["status"] = "active"
        manifest["versions"][version]["activated_at"] = datetime.now().isoformat()

    if old_version and old_version != version:
        is_upgrade = compare_versions(version, old_version) > 0
        manifest.setdefault("update_history", []).append(
            {
                "from": old_version,
                "to": version,
                "timestamp": datetime.now().isoformat(),
                "type": "upgrade" if is_upgrade else "rollback",
            }
        )

    save_manifest(home, manifest)


def verify_setup_complete(home: Path) -> bool:
    """Check if privileged setup has been completed."""
    try:
        return get_manifest(home).get("setup_complete", False)
    except FileNotFoundError:
        return False


def get_version_info(home: Path, version: str) -> Optional[Dict[str, Any]]:
    """Get info about a specific version."""
    try:
        return get_manifest(home).get("versions", {}).get(version)
    except FileNotFoundError:
        return None


def get_update_history(home: Path) -> list:
    """Get the update history from manifest."""
    try:
        return get_manifest(home).get("update_history", [])
    except FileNotFoundError:
        return []
