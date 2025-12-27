"""
Backup and restore functionality for SyrvisCore.

Provides:
- Automatic backup on upgrade (captures current state before changes)
- Post-setup backup (captures configured state with -N suffix)
- Full restore for rollback and disaster recovery
- Version-aware backup cleanup

Backup naming convention:
    0.1.12.tar.gz      - Pre-upgrade backup (before upgrading FROM 0.1.12)
    0.1.12-1.tar.gz    - Post-setup backup #1
    0.1.12-2.tar.gz    - Post-setup backup #2
"""

import json
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from . import paths
from . import manifest
from .__version__ import __version__


# Backup metadata schema version
BACKUP_SCHEMA_VERSION = 1


def get_backups_dir() -> Path:
    """Get the backups directory path."""
    return paths.get_syrvis_home() / "backups"


def ensure_backups_dir() -> Path:
    """Ensure backups directory exists and return path."""
    backups_dir = get_backups_dir()
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def get_backup_path(version: str, suffix: Optional[int] = None) -> Path:
    """
    Get the path for a backup file.

    Args:
        version: Version string (e.g., "0.1.12")
        suffix: Optional numeric suffix for post-setup backups (e.g., 1, 2)

    Returns:
        Path like backups/0.1.12.tar.gz or backups/0.1.12-1.tar.gz
    """
    if suffix is not None:
        filename = f"{version}-{suffix}.tar.gz"
    else:
        filename = f"{version}.tar.gz"
    return get_backups_dir() / filename


def parse_backup_filename(filename: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parse a backup filename to extract version and suffix.

    Args:
        filename: Backup filename (e.g., "0.1.12.tar.gz" or "0.1.12-2.tar.gz")

    Returns:
        Tuple of (version, suffix) where suffix is None for base backups
    """
    # Match patterns like "0.1.12.tar.gz" or "0.1.12-2.tar.gz"
    match = re.match(r'^(\d+\.\d+\.\d+)(?:-(\d+))?\.tar\.gz$', filename)
    if match:
        version = match.group(1)
        suffix = int(match.group(2)) if match.group(2) else None
        return version, suffix
    return None, None


def list_backups() -> List[Dict[str, Any]]:
    """
    List all available backups with metadata.

    Returns:
        List of backup info dicts, sorted by version (newest first), then suffix
    """
    backups_dir = get_backups_dir()
    if not backups_dir.exists():
        return []

    backups = []
    for backup_file in backups_dir.glob("*.tar.gz"):
        version, suffix = parse_backup_filename(backup_file.name)
        if version is None:
            continue

        # Try to read metadata from archive
        metadata = None
        try:
            with tarfile.open(backup_file, "r:gz") as tar:
                try:
                    meta_file = tar.extractfile("backup-metadata.json")
                    if meta_file:
                        metadata = json.loads(meta_file.read().decode())
                except (KeyError, json.JSONDecodeError):
                    pass
        except (tarfile.TarError, OSError):
            pass

        backup_info = {
            "path": backup_file,
            "filename": backup_file.name,
            "version": version,
            "suffix": suffix,
            "size": backup_file.stat().st_size,
            "created_at": metadata.get("created_at") if metadata else None,
            "reason": metadata.get("reason") if metadata else "unknown",
            "metadata": metadata,
        }
        backups.append(backup_info)

    # Sort by version (newest first), then by suffix (base first, then ascending)
    def sort_key(b):
        version_parts = tuple(int(p) for p in b["version"].split("."))
        suffix = b["suffix"] if b["suffix"] is not None else -1
        return (version_parts, suffix)

    return sorted(backups, key=sort_key, reverse=True)


def list_backup_versions() -> List[str]:
    """
    Get list of unique versions that have backups.

    Returns:
        List of version strings, sorted newest first
    """
    backups = list_backups()
    versions = sorted(set(b["version"] for b in backups), reverse=True,
                      key=lambda v: tuple(int(p) for p in v.split(".")))
    return versions


def get_next_suffix(version: str) -> int:
    """
    Get the next available suffix number for a version.

    Args:
        version: Version string

    Returns:
        Next suffix number (1 if no suffixed backups exist)
    """
    backups_dir = get_backups_dir()
    if not backups_dir.exists():
        return 1

    existing_suffixes = []
    for backup_file in backups_dir.glob(f"{version}-*.tar.gz"):
        _, suffix = parse_backup_filename(backup_file.name)
        if suffix is not None:
            existing_suffixes.append(suffix)

    if not existing_suffixes:
        return 1
    return max(existing_suffixes) + 1


def get_wheel_path(version: str) -> Optional[Path]:
    """
    Get the cached wheel file path for a version.

    Args:
        version: Version string

    Returns:
        Path to wheel file, or None if not found
    """
    version_dir = paths.get_version_dir(version)
    wheel_dir = version_dir / "wheel"

    if not wheel_dir.exists():
        return None

    wheels = list(wheel_dir.glob("*.whl"))
    if wheels:
        return wheels[0]
    return None


def create_backup(
    output_path: Optional[Path] = None,
    version: Optional[str] = None,
    reason: str = "manual",
    suffix: Optional[int] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Create a backup archive of the current state.

    Args:
        output_path: Where to save the backup (default: backups/<version>.tar.gz)
        version: Version to backup (default: current active version)
        reason: Reason for backup ("manual", "pre-upgrade", "post-setup")
        suffix: Numeric suffix for the filename (for post-setup backups)
        extra_metadata: Additional metadata to include

    Returns:
        Path to created backup file

    Raises:
        ValueError: If no version is active and none specified
    """
    syrvis_home = paths.get_syrvis_home()

    # Determine version
    if version is None:
        version = manifest.get_active_version()
        if version is None:
            raise ValueError("No active version and none specified")

    # Determine output path
    if output_path is None:
        output_path = get_backup_path(version, suffix)

    # Always ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build metadata
    metadata = {
        "backup_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(),
        "version": version,
        "manager_version": __version__,
        "reason": reason,
        "syrvis_home": str(syrvis_home),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    # Create backup archive
    with tarfile.open(output_path, "w:gz") as tar:
        # Add metadata
        metadata_json = json.dumps(metadata, indent=2).encode()
        meta_info = tarfile.TarInfo(name="backup-metadata.json")
        meta_info.size = len(metadata_json)
        meta_info.mtime = int(datetime.now().timestamp())
        tar.addfile(meta_info, fileobj=__import__("io").BytesIO(metadata_json))

        # Add manifest
        manifest_path = syrvis_home / ".syrviscore-manifest.json"
        if manifest_path.exists():
            tar.add(manifest_path, arcname="manifest.json")

        # Add config directory
        config_dir = syrvis_home / "config"
        if config_dir.exists():
            for item in config_dir.rglob("*"):
                if item.is_file():
                    arcname = f"config/{item.relative_to(config_dir)}"
                    tar.add(item, arcname=arcname)

        # Add data directories (selective - skip logs)
        data_items = [
            ("data/traefik/acme.json", syrvis_home / "data/traefik/acme.json"),
            ("data/traefik/traefik.yml", syrvis_home / "data/traefik/traefik.yml"),
        ]

        # Add Traefik config directory
        traefik_config = syrvis_home / "data/traefik/config"
        if traefik_config.exists():
            for item in traefik_config.rglob("*"):
                if item.is_file():
                    arcname = f"data/traefik/config/{item.relative_to(traefik_config)}"
                    data_items.append((arcname, item))

        # Add Portainer data
        portainer_dir = syrvis_home / "data/portainer"
        if portainer_dir.exists():
            for item in portainer_dir.rglob("*"):
                if item.is_file():
                    arcname = f"data/portainer/{item.relative_to(portainer_dir)}"
                    data_items.append((arcname, item))

        # Add Cloudflared data
        cloudflared_dir = syrvis_home / "data/cloudflared"
        if cloudflared_dir.exists():
            for item in cloudflared_dir.rglob("*"):
                if item.is_file():
                    arcname = f"data/cloudflared/{item.relative_to(cloudflared_dir)}"
                    data_items.append((arcname, item))

        for arcname, src_path in data_items:
            if isinstance(src_path, Path) and src_path.exists():
                tar.add(src_path, arcname=arcname)

        # Add wheel file for the version
        wheel_path = get_wheel_path(version)
        if wheel_path and wheel_path.exists():
            tar.add(wheel_path, arcname=f"wheel/{wheel_path.name}")

    return output_path


def create_pre_upgrade_backup(current_version: str, target_version: str) -> Optional[Path]:
    """
    Create a backup before upgrading to a new version.

    Only creates backup if one doesn't already exist for this version.

    Args:
        current_version: Version currently installed
        target_version: Version being upgraded to

    Returns:
        Path to backup file, or None if backup already exists
    """
    backup_path = get_backup_path(current_version)

    # If backup already exists, don't overwrite
    if backup_path.exists():
        return None

    return create_backup(
        output_path=backup_path,
        version=current_version,
        reason="pre-upgrade",
        extra_metadata={"upgraded_to": target_version},
    )


def create_post_setup_backup(version: str) -> Path:
    """
    Create a backup after successful setup.

    Uses -N suffix to allow multiple post-setup backups.

    Args:
        version: Version that was set up

    Returns:
        Path to backup file
    """
    suffix = get_next_suffix(version)
    return create_backup(
        version=version,
        reason="post-setup",
        suffix=suffix,
    )


def restore_from_backup(
    backup_path: Path,
    install_path: Optional[Path] = None,
) -> bool:
    """
    Restore from a backup archive.

    Args:
        backup_path: Path to backup archive
        install_path: Where to restore (default: from backup metadata)

    Returns:
        True if restore succeeded
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    with tarfile.open(backup_path, "r:gz") as tar:
        # Read metadata
        try:
            meta_file = tar.extractfile("backup-metadata.json")
            if meta_file:
                metadata = json.loads(meta_file.read().decode())
            else:
                raise ValueError("Backup missing metadata")
        except (KeyError, json.JSONDecodeError) as e:
            raise ValueError(f"Invalid backup metadata: {e}")

        version = metadata.get("version")
        if not version:
            raise ValueError("Backup metadata missing version")

        # Determine install path
        if install_path is None:
            install_path = Path(metadata.get("syrvis_home", "/volume1/syrviscore"))

        # Create base directory structure
        install_path.mkdir(parents=True, exist_ok=True)

        # Extract config and data
        for member in tar.getmembers():
            # Skip metadata file
            if member.name == "backup-metadata.json":
                continue

            # Determine destination
            if member.name.startswith("config/") or member.name.startswith("data/"):
                dest = install_path / member.name
            elif member.name == "manifest.json":
                dest = install_path / ".syrviscore-manifest.json"
            elif member.name.startswith("wheel/"):
                # Extract wheel to version directory
                version_dir = install_path / "versions" / version
                version_dir.mkdir(parents=True, exist_ok=True)
                wheel_dir = version_dir / "wheel"
                wheel_dir.mkdir(exist_ok=True)
                wheel_name = Path(member.name).name
                dest = wheel_dir / wheel_name
            else:
                continue

            # Extract file
            if member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src:
                    if src:
                        dest.write_bytes(src.read())
                # Preserve permissions for sensitive files
                if "acme.json" in str(dest):
                    dest.chmod(0o600)
                elif dest.suffix in (".sh", ""):
                    dest.chmod(0o755)
                else:
                    dest.chmod(0o644)

        # Install version from wheel if not already installed
        version_venv = install_path / "versions" / version / "cli" / "venv"
        if not version_venv.exists():
            wheel_dir = install_path / "versions" / version / "wheel"
            wheels = list(wheel_dir.glob("*.whl")) if wheel_dir.exists() else []
            if wheels:
                install_version_from_wheel(version, wheels[0], install_path)

        # Update current symlink
        paths.update_current_symlink(version)

        # Update manifest
        manifest.set_active_version(version)

    return True


def install_version_from_wheel(version: str, wheel_path: Path, install_path: Path) -> bool:
    """
    Install a version from a wheel file.

    Args:
        version: Version string
        wheel_path: Path to wheel file
        install_path: SYRVIS_HOME path

    Returns:
        True if installation succeeded
    """
    import subprocess

    version_dir = install_path / "versions" / version
    version_dir.mkdir(parents=True, exist_ok=True)

    cli_dir = version_dir / "cli"
    cli_dir.mkdir(exist_ok=True)

    venv_path = cli_dir / "venv"

    # Create virtual environment
    result = subprocess.run(
        ["python3", "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    # Install wheel
    pip_path = venv_path / "bin" / "pip"
    result = subprocess.run(
        [str(pip_path), "install", "--no-cache-dir", str(wheel_path)],
        capture_output=True,
        text=True,
    )

    return result.returncode == 0


def cleanup_old_backups(keep_versions: int = 3, dry_run: bool = False) -> List[Path]:
    """
    Remove old backups, keeping the most recent N versions.

    Args:
        keep_versions: Number of versions to keep (keeps all backups for those versions)
        dry_run: If True, only return what would be deleted

    Returns:
        List of backup paths that were (or would be) deleted
    """
    backups = list_backups()
    if not backups:
        return []

    # Get unique versions, sorted newest first
    all_versions = []
    seen = set()
    for b in backups:
        if b["version"] not in seen:
            all_versions.append(b["version"])
            seen.add(b["version"])

    # Sort by semantic version (newest first)
    all_versions.sort(
        key=lambda v: tuple(int(p) for p in v.split(".")),
        reverse=True
    )

    # Determine versions to keep
    versions_to_keep = set(all_versions[:keep_versions])

    # Find backups to delete
    to_delete = []
    for backup in backups:
        if backup["version"] not in versions_to_keep:
            to_delete.append(backup["path"])

    # Delete if not dry run
    if not dry_run:
        for path in to_delete:
            path.unlink()

    return to_delete


def get_backup_for_rollback(version: str) -> Optional[Path]:
    """
    Get the backup file to use for rolling back to a version.

    Prefers the base backup (no suffix), falls back to highest suffix.

    Args:
        version: Version to rollback to

    Returns:
        Path to backup file, or None if not found
    """
    # Try base backup first
    base_backup = get_backup_path(version)
    if base_backup.exists():
        return base_backup

    # Look for suffixed backups
    backups_dir = get_backups_dir()
    if not backups_dir.exists():
        return None

    suffixed_backups = []
    for backup_file in backups_dir.glob(f"{version}-*.tar.gz"):
        _, suffix = parse_backup_filename(backup_file.name)
        if suffix is not None:
            suffixed_backups.append((suffix, backup_file))

    if suffixed_backups:
        # Return the one with highest suffix (most recent setup)
        suffixed_backups.sort(reverse=True)
        return suffixed_backups[0][1]

    return None
