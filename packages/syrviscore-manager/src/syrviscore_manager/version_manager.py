"""
Version manager for SyrvisCore.

Handles installation, activation, and rollback of service versions.
Integrates with backup system for safe upgrades and rollbacks.
"""

import os
import sys
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import click

from . import paths
from . import manifest
from . import downloader
from . import backup


def set_readable_permissions(path: Path) -> None:
    """Set directory permissions to be readable by all users (755)."""
    try:
        # Set directory to 755 (rwxr-xr-x)
        os.chmod(path, 0o755)
    except OSError:
        pass  # Ignore permission errors


def set_tree_readable(path: Path) -> None:
    """Recursively set all directories to 755 and files to 644 (bin/* to 755)."""
    try:
        for root, dirs, files in os.walk(path):
            root_path = Path(root)
            # Set directory permissions
            os.chmod(root_path, 0o755)
            # Set file permissions
            for f in files:
                file_path = root_path / f
                if root_path.name == "bin":
                    os.chmod(file_path, 0o755)  # Executables
                else:
                    os.chmod(file_path, 0o644)  # Regular files
    except OSError:
        pass  # Ignore permission errors


def cache_wheel_file(version: str, wheel_path: Path) -> Optional[Path]:
    """
    Cache a wheel file in the version directory for backup/restore.

    Args:
        version: Version string
        wheel_path: Path to the wheel file

    Returns:
        Path to cached wheel, or None if caching failed
    """
    try:
        version_dir = paths.get_version_dir(version)
        wheel_cache = version_dir / "wheel"
        wheel_cache.mkdir(parents=True, exist_ok=True)

        cached_path = wheel_cache / wheel_path.name
        shutil.copy(wheel_path, cached_path)
        return cached_path
    except Exception:
        return None


def install_version(version: str, wheel_path: Path, config_path: Optional[Path] = None) -> bool:
    """
    Install a service version from wheel file.

    Args:
        version: Version string
        wheel_path: Path to the wheel file
        config_path: Optional path to config.yaml

    Returns:
        True if installation succeeded
    """
    try:
        syrvis_home = paths.get_syrvis_home_or_create()
        version_dir = paths.get_version_dir(version)

        # Create version directory structure
        paths.ensure_directory_structure(syrvis_home, version)

        # Set readable permissions on key directories
        set_readable_permissions(syrvis_home)
        set_readable_permissions(syrvis_home / "versions")
        set_readable_permissions(syrvis_home / "config")
        set_readable_permissions(syrvis_home / "data")
        set_readable_permissions(syrvis_home / "bin")

        # Cache wheel file for backup/restore
        cache_wheel_file(version, wheel_path)

        # Copy wheel file to cli directory for installation
        cli_dir = version_dir / "cli"
        wheel_dest = cli_dir / wheel_path.name
        shutil.copy(wheel_path, wheel_dest)

        # Copy config.yaml if provided
        if config_path and config_path.exists():
            build_dir = version_dir / "build"
            shutil.copy(config_path, build_dir / "config.yaml")

        # Create venv
        venv_path = cli_dir / "venv"
        if not venv_path.exists():
            click.echo("      Creating virtual environment...")
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                click.echo(f"      Error creating venv: {result.stderr}", err=True)
                return False

        # Upgrade pip
        pip_path = venv_path / "bin" / "pip"
        subprocess.run(
            [str(pip_path), "install", "--upgrade", "pip", "--quiet"],
            capture_output=True
        )

        # Install wheel into venv
        click.echo("      Installing service package...")
        result = subprocess.run(
            [str(pip_path), "install", "--quiet", str(wheel_dest)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            click.echo(f"      Error installing wheel: {result.stderr}", err=True)
            return False

        # Verify installation
        syrvis_bin = venv_path / "bin" / "syrvis"
        if not syrvis_bin.exists():
            click.echo("      Error: syrvis command not found after install", err=True)
            return False

        # Set readable permissions on entire version directory tree
        # (directories created as root may have restrictive permissions)
        set_tree_readable(version_dir)

        # Add version to manifest
        manifest.add_version_to_manifest(version, "available")

        return True

    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def uninstall_version(version: str) -> bool:
    """
    Uninstall a service version.

    Args:
        version: Version to uninstall

    Returns:
        True if uninstallation succeeded
    """
    try:
        # Check if this is the active version
        active = manifest.get_active_version()
        if version == active:
            click.echo(f"      Cannot uninstall active version: {version}", err=True)
            click.echo("      Use 'syrvisctl activate <other-version>' first", err=True)
            return False

        version_dir = paths.get_version_dir(version)
        if not version_dir.exists():
            click.echo(f"      Version {version} not found", err=True)
            return False

        # Remove directory
        shutil.rmtree(version_dir)

        # Remove from manifest
        manifest.remove_version_from_manifest(version)

        return True

    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def activate_version(version: str) -> bool:
    """
    Activate a service version (update symlink and manifest).

    Args:
        version: Version to activate

    Returns:
        True if activation succeeded
    """
    try:
        version_dir = paths.get_version_dir(version)
        if not version_dir.exists():
            click.echo(f"      Error: Version {version} not installed", err=True)
            return False

        # Verify venv exists
        venv_path = version_dir / "cli" / "venv"
        if not venv_path.exists():
            click.echo(f"      Error: Version {version} is incomplete (no venv)", err=True)
            return False

        # Update symlink
        paths.update_current_symlink(version)

        # Update wrapper script and profile
        paths.create_syrvis_wrapper()
        paths.create_syrvis_profile()

        # Update manifest
        manifest.set_active_version(version)

        return True

    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def get_previous_version() -> Optional[str]:
    """Get the previous version for rollback."""
    try:
        active = manifest.get_active_version()
        versions = paths.list_installed_versions()

        if len(versions) < 2:
            return None

        # Find first version that isn't active
        for v in versions:
            if v != active:
                return v

        return None

    except paths.SyrvisHomeError:
        return None


def download_and_install(version: Optional[str] = None, force: bool = False) -> bool:
    """
    Download and install a service version from GitHub.

    Creates a backup of the current state before upgrading.

    Args:
        version: Specific version to install, or None for latest
        force: Force reinstall if version exists

    Returns:
        True if installation succeeded
    """
    # Check for existing installation to backup
    current_version = manifest.get_active_version()

    # Fetch release info
    if not version:
        click.echo("[1/5] Fetching latest release...")
        release = downloader.get_latest_release()
        if not release:
            click.echo("      Could not fetch release information", err=True)
            return False
        version = downloader.get_version_from_release(release)
    else:
        version = version.lstrip('v')
        click.echo(f"[1/5] Fetching release v{version}...")
        release = downloader.get_release_by_tag(version)
        if not release:
            click.echo(f"      Release v{version} not found", err=True)
            return False

    click.echo(f"      Version: {version}")

    # Create pre-upgrade backup if we have an existing installation
    if current_version and current_version != version:
        click.echo()
        click.echo("[2/5] Creating backup of current state...")
        try:
            backup_path = backup.create_pre_upgrade_backup(current_version, version)
            if backup_path:
                click.echo(f"      Backup: {backup_path}")
            else:
                click.echo(f"      Backup already exists for {current_version}")
        except Exception as e:
            click.echo(f"      Warning: Could not create backup: {e}")
            # Continue with install - backup failure shouldn't block upgrade
    else:
        click.echo()
        click.echo("[2/5] No existing version to backup")

    # Check if already installed
    try:
        version_dir = paths.get_version_dir(version)
        if version_dir.exists():
            if not force:
                click.echo(f"      Version {version} already installed")
                if not click.confirm("      Reinstall?", default=False):
                    return True  # Not an error, just skip
            # Remove existing version directory for reinstall
            shutil.rmtree(version_dir)
    except paths.SyrvisHomeError:
        pass  # Will create directory structure

    # Find wheel asset
    wheel_asset = downloader.find_wheel_asset(release)
    if not wheel_asset:
        click.echo("      No wheel file found in release", err=True)
        click.echo("      Looking for: syrviscore-*.whl", err=True)
        return False

    # Download to temp directory
    click.echo()
    click.echo("[3/5] Downloading...")
    click.echo(f"      {wheel_asset['name']}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Download wheel
        wheel_path = tmp_path / wheel_asset["name"]
        if not downloader.download_file(wheel_asset["browser_download_url"], wheel_path):
            return False

        # Download config.yaml if available
        config_path = None
        config_asset = downloader.find_config_asset(release)
        if config_asset:
            config_path = tmp_path / "config.yaml"
            click.echo("      config.yaml")
            downloader.download_file(
                config_asset["browser_download_url"],
                config_path,
                show_progress=False
            )

        # Install
        click.echo()
        click.echo("[4/5] Installing...")

        if not install_version(version, wheel_path, config_path):
            click.echo("      Installation failed", err=True)
            return False

        click.echo("      Installed successfully")

    # Activate
    click.echo()
    click.echo("[5/5] Activating version...")

    if not activate_version(version):
        click.echo("      Activation failed", err=True)
        return False

    click.echo(f"      Activated: {version}")

    return True


def rollback_to_backup(version: str) -> bool:
    """
    Perform a full rollback to a version using its backup.

    Restores both code and configuration from the backup archive.

    Args:
        version: Version to rollback to

    Returns:
        True if rollback succeeded
    """
    # Find backup for this version
    backup_path = backup.get_backup_for_rollback(version)
    if not backup_path:
        click.echo(f"      No backup found for version {version}", err=True)
        return False

    click.echo(f"      Using backup: {backup_path.name}")

    try:
        syrvis_home = paths.get_syrvis_home()

        # Restore from backup
        click.echo("      Restoring configuration and data...")
        backup.restore_from_backup(backup_path, syrvis_home)

        # Update wrapper and profile
        paths.create_syrvis_wrapper()
        paths.create_syrvis_profile()

        return True

    except Exception as e:
        click.echo(f"      Error during rollback: {e}", err=True)
        return False


def cleanup_old_versions(keep: int = 2, dry_run: bool = False) -> list:
    """
    Remove old versions to free disk space.

    Args:
        keep: Number of versions to keep (including active)
        dry_run: If True, don't actually remove anything

    Returns:
        List of versions that were (or would be) removed
    """
    try:
        versions = paths.list_installed_versions()
        active = manifest.get_active_version()
    except paths.SyrvisHomeError:
        return []

    if len(versions) <= keep:
        return []

    # Determine versions to remove (keep newest N, never remove active)
    to_remove = []
    kept = 0
    for v in versions:
        if v == active:
            continue
        if kept < keep - 1:  # -1 because active counts as kept
            kept += 1
            continue
        to_remove.append(v)

    if dry_run:
        return to_remove

    # Remove versions
    removed = []
    for v in to_remove:
        if uninstall_version(v):
            removed.append(v)

    return removed
