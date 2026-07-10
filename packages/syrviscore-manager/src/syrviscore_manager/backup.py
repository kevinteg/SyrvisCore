"""
Backup and restore functionality for SyrvisCore.

Provides:
- Automatic backup on upgrade (captures current state before changes)
- Post-setup backup (captures configured state with -N suffix)
- Full restore for rollback and disaster recovery
- Version-aware backup cleanup

v2 rules:
- Archives contain secrets (acme.json, Cloudflared credentials) and are
  created with mode 0600.
- Extraction is containment-checked: no archive member may escape the
  install path, regardless of what the archive claims its name is.
- Restore threads the install path through every step explicitly — it can
  never silently touch a different installation.
- File modes are restored from the archive (with acme.json forced to 0600),
  never guessed from filename suffixes.

Backup naming convention:
    0.1.12.tar.gz      - Pre-upgrade backup (before upgrading FROM 0.1.12)
    0.1.12-1.tar.gz    - Post-setup backup #1
    0.1.12-2.tar.gz    - Post-setup backup #2
"""

import io
import json
import os
import re
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import manifest, paths
from .__version__ import __version__
from .errors import BackupError, RestoreError

# Backup metadata schema version
BACKUP_SCHEMA_VERSION = 1

LogCallback = Callable[[str], None]


def _noop_log(_message: str) -> None:
    return None


def get_backups_dir(home: Path) -> Path:
    """Get the backups directory path."""
    return home / "backups"


def get_backup_path(home: Path, version: str, suffix: Optional[int] = None) -> Path:
    """Get the path for a backup file (backups/<version>[-<suffix>].tar.gz)."""
    if suffix is not None:
        filename = "{}-{}.tar.gz".format(version, suffix)
    else:
        filename = "{}.tar.gz".format(version)
    return get_backups_dir(home) / filename


def parse_backup_filename(filename: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse a backup filename into (version, suffix); suffix None for base backups."""
    match = re.match(r"^(\d+\.\d+\.\d+)(?:-(\d+))?\.tar\.gz$", filename)
    if match:
        return match.group(1), int(match.group(2)) if match.group(2) else None
    return None, None


def list_backups(home: Path) -> List[Dict[str, Any]]:
    """List all available backups with metadata, newest version first."""
    backups_dir = get_backups_dir(home)
    if not backups_dir.exists():
        return []

    backups = []
    for backup_file in backups_dir.glob("*.tar.gz"):
        version, suffix = parse_backup_filename(backup_file.name)
        if version is None:
            continue

        metadata = None
        try:
            with tarfile.open(str(backup_file), "r:gz") as tar:
                try:
                    meta_file = tar.extractfile("backup-metadata.json")
                    if meta_file:
                        metadata = json.loads(meta_file.read().decode())
                except (KeyError, json.JSONDecodeError):
                    pass
        except (tarfile.TarError, OSError):
            pass

        backups.append(
            {
                "path": backup_file,
                "filename": backup_file.name,
                "version": version,
                "suffix": suffix,
                "size": backup_file.stat().st_size,
                "created_at": metadata.get("created_at") if metadata else None,
                "reason": metadata.get("reason") if metadata else "unknown",
                "metadata": metadata,
            }
        )

    def sort_key(b):
        version_parts = tuple(int(p) for p in b["version"].split("."))
        suffix = b["suffix"] if b["suffix"] is not None else -1
        return (version_parts, suffix)

    return sorted(backups, key=sort_key, reverse=True)


def get_next_suffix(home: Path, version: str) -> int:
    """Get the next available suffix number for a version's backups."""
    backups_dir = get_backups_dir(home)
    if not backups_dir.exists():
        return 1

    existing = []
    for backup_file in backups_dir.glob("{}-*.tar.gz".format(version)):
        _, suffix = parse_backup_filename(backup_file.name)
        if suffix is not None:
            existing.append(suffix)

    return max(existing) + 1 if existing else 1


def get_wheel_path(home: Path, version: str) -> Optional[Path]:
    """Get the cached wheel file path for an installed version."""
    wheel_dir = paths.version_dir(home, version) / "wheel"
    if not wheel_dir.exists():
        return None
    wheels = list(wheel_dir.glob("*.whl"))
    return wheels[0] if wheels else None


def create_backup(
    home: Path,
    output_path: Optional[Path] = None,
    version: Optional[str] = None,
    reason: str = "manual",
    suffix: Optional[int] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Create a backup archive of the current state.

    The archive is created with mode 0600 (it contains ACME private keys and
    tunnel credentials).

    Raises:
        BackupError: If no version is active and none was specified.
    """
    if version is None:
        version = manifest.get_active_version(home)
        if version is None:
            raise BackupError("No active version and none specified")

    if output_path is None:
        output_path = get_backup_path(home, version, suffix)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata = {
        "backup_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(),
        "version": version,
        "manager_version": __version__,
        "reason": reason,
        "syrvis_home": str(home),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    # 0600 from the moment of creation — never world-readable, even briefly
    fd = os.open(str(output_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fileobj:
            with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
                metadata_json = json.dumps(metadata, indent=2).encode()
                meta_info = tarfile.TarInfo(name="backup-metadata.json")
                meta_info.size = len(metadata_json)
                meta_info.mtime = int(datetime.now().timestamp())
                tar.addfile(meta_info, fileobj=io.BytesIO(metadata_json))

                mpath = paths.manifest_path(home)
                if mpath.exists():
                    tar.add(str(mpath), arcname="manifest.json")

                config_dir = home / "config"
                if config_dir.exists():
                    for item in sorted(config_dir.rglob("*")):
                        if item.is_file():
                            tar.add(
                                str(item),
                                arcname="config/{}".format(item.relative_to(config_dir)),
                            )

                data_items = [
                    ("data/traefik/acme.json", home / "data/traefik/acme.json"),
                    ("data/traefik/traefik.yml", home / "data/traefik/traefik.yml"),
                ]
                for subdir in ("data/traefik/config", "data/portainer", "data/cloudflared"):
                    root = home / subdir
                    if root.exists():
                        for item in sorted(root.rglob("*")):
                            if item.is_file():
                                data_items.append(
                                    ("{}/{}".format(subdir, item.relative_to(root)), item)
                                )

                for arcname, src_path in data_items:
                    if src_path.exists():
                        tar.add(str(src_path), arcname=arcname)

                wheel_path = get_wheel_path(home, version)
                if wheel_path and wheel_path.exists():
                    tar.add(str(wheel_path), arcname="wheel/{}".format(wheel_path.name))
    except BaseException:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise

    return output_path


def create_pre_upgrade_backup(
    home: Path, current_version: str, target_version: str
) -> Optional[Path]:
    """Create a backup before upgrading, unless one already exists for this version."""
    backup_path = get_backup_path(home, current_version)
    if backup_path.exists():
        return None

    return create_backup(
        home,
        output_path=backup_path,
        version=current_version,
        reason="pre-upgrade",
        extra_metadata={"upgraded_to": target_version},
    )


def create_post_setup_backup(home: Path, version: str) -> Path:
    """Create a backup after successful setup (with -N suffix)."""
    return create_backup(
        home, version=version, reason="post-setup", suffix=get_next_suffix(home, version)
    )


def read_backup_metadata(backup_path: Path) -> Dict[str, Any]:
    """Read and validate the metadata of a backup archive.

    Raises:
        RestoreError: If the archive is unreadable or has no valid metadata.
    """
    if not backup_path.exists():
        raise RestoreError("Backup not found: {}".format(backup_path))
    try:
        with tarfile.open(str(backup_path), "r:gz") as tar:
            meta_file = tar.extractfile("backup-metadata.json")
            if not meta_file:
                raise RestoreError("Backup missing backup-metadata.json")
            metadata = json.loads(meta_file.read().decode())
    except (tarfile.TarError, KeyError, json.JSONDecodeError, OSError) as e:
        raise RestoreError("Could not read backup {}: {}".format(backup_path, e))

    if not metadata.get("version"):
        raise RestoreError("Backup metadata missing version")
    return metadata


def _safe_dest(install_path: Path, relative: str) -> Path:
    """Resolve an archive member path, refusing anything that escapes install_path."""
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise RestoreError(
            "Backup archive contains an unsafe path {!r} — refusing to restore".format(relative)
        )
    dest = install_path / relative
    # Belt and braces: verify containment on the normalized path as well
    base = os.path.realpath(str(install_path))
    resolved_parent = os.path.realpath(str(dest.parent))
    if not (resolved_parent == base or resolved_parent.startswith(base + os.sep)):
        raise RestoreError(
            "Backup archive member {!r} escapes the install path — refusing to restore".format(
                relative
            )
        )
    return dest


def restore_from_backup(
    backup_path: Path, install_path: Path, log: LogCallback = _noop_log
) -> Dict[str, Any]:
    """
    Restore from a backup archive into ``install_path``.

    The version's venv is rebuilt from the cached wheel and verified BEFORE
    the ``current`` symlink is switched — a restore can never claim success
    while leaving a non-runnable installation active.

    Returns:
        The backup metadata dict.

    Raises:
        RestoreError: On unsafe/invalid archives or a failed venv rebuild.
    """
    metadata = read_backup_metadata(backup_path)
    version = paths.validate_version(metadata["version"])

    install_path.mkdir(parents=True, exist_ok=True)

    with tarfile.open(str(backup_path), "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "backup-metadata.json":
                continue
            if not member.isfile():
                continue

            if member.name.startswith("config/") or member.name.startswith("data/"):
                dest = _safe_dest(install_path, member.name)
            elif member.name == "manifest.json":
                dest = install_path / paths.MANIFEST_FILENAME
            elif member.name.startswith("wheel/"):
                wheel_name = Path(member.name).name
                dest = _safe_dest(install_path, "versions/{}/wheel/{}".format(version, wheel_name))
            else:
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src:
                dest.write_bytes(src.read())

            # Restore the recorded mode; secrets are always clamped to 0600
            if dest.name == "acme.json" or "cloudflared" in dest.parts:
                dest.chmod(0o600)
            else:
                dest.chmod(member.mode & 0o777 or 0o644)

    # Rebuild the venv from the cached wheel if needed (single install path)
    version_venv = install_path / "versions" / version / "cli" / "venv"
    if not (version_venv / "bin" / "syrvis").exists():
        wheel_dir = install_path / "versions" / version / "wheel"
        wheels = list(wheel_dir.glob("*.whl")) if wheel_dir.exists() else []
        if not wheels:
            raise RestoreError(
                "Backup for {} contains no cached wheel and no venv exists; "
                "run 'syrvisctl install {}' after restore".format(version, version)
            )
        log("Rebuilding venv for {} from cached wheel...".format(version))
        from . import version_manager  # local import to avoid a module cycle

        version_manager.install_version(install_path, version, wheels[0], force=True, log=log)

    # Venv verified — now make the version active
    paths.update_current_symlink(install_path, version)
    paths.create_syrvis_wrapper(install_path)
    paths.create_syrvis_profile(install_path)
    manifest.set_active_version(install_path, version)

    return metadata


def cleanup_old_backups(home: Path, keep_versions: int = 3, dry_run: bool = False) -> List[Path]:
    """Remove old backups, keeping all backups for the most recent N versions."""
    backups = list_backups(home)
    if not backups:
        return []

    all_versions = []
    seen = set()
    for b in backups:
        if b["version"] not in seen:
            all_versions.append(b["version"])
            seen.add(b["version"])

    all_versions.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    versions_to_keep = set(all_versions[:keep_versions])

    to_delete = [b["path"] for b in backups if b["version"] not in versions_to_keep]

    if not dry_run:
        for path in to_delete:
            path.unlink()

    return to_delete


def get_backup_for_rollback(home: Path, version: str) -> Optional[Path]:
    """
    Get the backup file to use for rolling back to a version.

    Prefers the base backup (no suffix), falls back to the highest suffix.
    """
    base_backup = get_backup_path(home, version)
    if base_backup.exists():
        return base_backup

    backups_dir = get_backups_dir(home)
    if not backups_dir.exists():
        return None

    suffixed = []
    for backup_file in backups_dir.glob("{}-*.tar.gz".format(version)):
        _, suffix = parse_backup_filename(backup_file.name)
        if suffix is not None:
            suffixed.append((suffix, backup_file))

    if suffixed:
        suffixed.sort(reverse=True)
        return suffixed[0][1]

    return None
