"""
SyrvisCore Manager CLI - syrvisctl command.

Provides version management for SyrvisCore service packages.

Commands:
    install [version]   - Download and install a service version
    uninstall <version> - Remove a service version
    list                - List installed versions
    activate <version>  - Switch active version
    rollback [version]  - Rollback to previous version (full restore)
    check               - Check for updates
    info                - Show installation info
    cleanup             - Remove old versions
    backup              - Backup management commands
    restore             - Restore from backup
"""

import sys
from pathlib import Path

import click

from .__version__ import __version__
from . import paths
from . import manifest
from . import downloader
from . import version_manager
from . import backup


@click.group()
@click.version_option(version=__version__, prog_name="syrvisctl")
def cli():
    """SyrvisCore Manager - Version management for SyrvisCore."""
    pass


def check_sudo_needed(path: Path) -> bool:
    """Check if we need sudo to write to the given path."""
    import os

    # Check if path exists and is writable
    if path.exists():
        return not os.access(path, os.W_OK)

    # Check parent directory
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent

    if parent.exists():
        return not os.access(parent, os.W_OK)

    return True  # Assume we need sudo if we can't determine


def reexec_with_sudo():
    """Re-execute the current command with sudo."""
    import os
    import shutil

    sudo_path = shutil.which("sudo")
    if not sudo_path:
        click.echo("Error: sudo not found", err=True)
        sys.exit(1)

    # Re-execute with sudo, preserving arguments
    args = [sudo_path, sys.executable] + sys.argv
    click.echo("  Elevated privileges required. Re-running with sudo...")
    click.echo()
    os.execv(sudo_path, args)


def run_syrvis_clean():
    """Run 'syrvis clean -y' to remove containers and networks."""
    import subprocess
    import shutil

    # Find syrvis command
    syrvis_paths = [
        paths.get_syrvis_home() / "bin" / "syrvis",
        paths.get_syrvis_home() / "current" / "cli" / "venv" / "bin" / "syrvis",
    ]

    syrvis_cmd = None
    for p in syrvis_paths:
        try:
            if p.exists():
                syrvis_cmd = str(p)
                break
        except Exception:
            pass

    if not syrvis_cmd:
        # Try PATH
        syrvis_cmd = shutil.which("syrvis")

    if not syrvis_cmd:
        return False, "syrvis command not found"

    try:
        result = subprocess.run(
            [syrvis_cmd, "clean", "-y"],
            capture_output=True,
            text=True,
            timeout=60
        )
        return True, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Clean operation timed out"
    except Exception as e:
        return False, str(e)


@cli.command()
@click.argument('version', required=False)
@click.option('--force', is_flag=True, help='Force reinstall even if version exists')
@click.option('--clean', is_flag=True, help='Clean Docker containers/networks before reinstall')
@click.option('--path', type=click.Path(), help='Installation path (default: auto-detect)')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompts')
def install(version, force, clean, path, yes):
    """Download and install a service version from GitHub.

    If VERSION is not specified, installs the latest release.

    Use --clean to remove existing Docker containers and networks before
    reinstalling. This is recommended when reinstalling to avoid conflicts.
    """
    import os

    click.echo()
    click.echo("Installing SyrvisCore service...")
    click.echo()

    # Clean Docker resources if requested
    if clean:
        click.echo("[0/4] Cleaning Docker resources...")
        try:
            success, output = run_syrvis_clean()
            if success:
                click.echo("      Containers and networks removed")
            else:
                click.echo(f"      Warning: Clean failed - {output}", err=True)
                click.echo("      Continuing with install...")
        except Exception as e:
            click.echo(f"      Warning: Clean failed - {e}", err=True)
            click.echo("      Continuing with install...")
        click.echo()

    # Determine installation path
    try:
        existing_home = paths.get_syrvis_home()
        install_path = existing_home
        click.echo(f"  Using existing installation: {install_path}")
    except paths.SyrvisHomeError:
        # New installation - prompt for path
        default_path = paths.get_default_install_path()

        if path:
            install_path = Path(path)
        elif yes:
            install_path = default_path
        else:
            # Prompt with bracket format: Installation path [/volume4/syrviscore]:
            user_path = click.prompt(
                f"  Installation path [{default_path}]",
                default=str(default_path),
                show_default=False
            )
            install_path = Path(user_path)

        click.echo(f"  Installing to: {install_path}")

    # Check if we need elevated privileges
    if check_sudo_needed(install_path):
        if os.geteuid() != 0:
            reexec_with_sudo()

    # Set SYRVIS_HOME for this session
    os.environ["SYRVIS_HOME"] = str(install_path)

    click.echo()

    if version_manager.download_and_install(version, force):
        click.echo()
        click.echo("Installation complete!")
        click.echo()

        # Show how to add syrvis to PATH
        try:
            profile_path = paths.get_syrvis_profile_path()
            click.echo("To add 'syrvis' to your PATH:")
            click.echo(f"  source {profile_path}")
            click.echo()
            click.echo("Or add to your shell profile for permanent access:")
            click.echo(f"  echo 'source {profile_path}' >> ~/.profile")
            click.echo()
        except paths.SyrvisHomeError:
            pass

        click.echo("Next steps:")
        click.echo("  1. Run 'syrvis setup' to configure the service")
        click.echo("  2. Run 'syrvis start' to start the services")
    else:
        sys.exit(1)


@cli.command()
@click.argument('version')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
def uninstall(version, yes):
    """Remove a service version.

    Cannot uninstall the currently active version.
    """
    click.echo()

    # Verify version exists
    version_dir = paths.get_version_dir(version)
    if not version_dir.exists():
        click.echo(f"Version {version} is not installed", err=True)
        sys.exit(1)

    # Check if active
    active = manifest.get_active_version()
    if version == active:
        click.echo(f"Cannot uninstall active version: {version}", err=True)
        click.echo("Use 'syrvisctl activate <other-version>' first", err=True)
        sys.exit(1)

    if not yes:
        if not click.confirm(f"Uninstall version {version}?"):
            click.echo("Uninstall cancelled")
            return

    click.echo(f"Uninstalling {version}...")

    if version_manager.uninstall_version(version):
        click.echo(f"Version {version} uninstalled")
    else:
        sys.exit(1)


@cli.command('list')
def list_versions():
    """List installed service versions."""
    click.echo()
    click.echo("Installed versions:")
    click.echo()

    try:
        versions = paths.list_installed_versions()
        active = manifest.get_active_version()
    except paths.SyrvisHomeError:
        click.echo("  No versions installed")
        click.echo()
        click.echo("Run 'syrvisctl install' to install a version")
        return

    if not versions:
        click.echo("  No versions installed")
        click.echo()
        click.echo("Run 'syrvisctl install' to install a version")
        return

    for v in versions:
        marker = " (active)" if v == active else ""
        click.echo(f"  {v}{marker}")

    click.echo()


@cli.command()
@click.argument('version')
def activate(version):
    """Activate a specific service version.

    Switches the 'current' symlink to point to the specified version.
    """
    click.echo()

    # Verify version exists
    version_dir = paths.get_version_dir(version)
    if not version_dir.exists():
        click.echo(f"Version {version} is not installed", err=True)
        click.echo()
        click.echo("Installed versions:")
        for v in paths.list_installed_versions():
            click.echo(f"  {v}")
        sys.exit(1)

    # Check if already active
    active = manifest.get_active_version()
    if version == active:
        click.echo(f"Version {version} is already active")
        return

    click.echo(f"Activating version {version}...")

    if version_manager.activate_version(version):
        click.echo(f"Activated: {version}")
        click.echo()
        click.echo("You may need to restart services:")
        click.echo("  syrvis restart")
    else:
        sys.exit(1)


@cli.command()
@click.argument('version', required=False)
def rollback(version):
    """Rollback to a previous version (full restore from backup).

    Restores both code AND configuration from the backup archive.
    This is a complete point-in-time restore.

    If VERSION is not specified, shows available backups to choose from.
    """
    click.echo()
    click.echo("SyrvisCore Rollback")
    click.echo("=" * 40)
    click.echo()

    active = manifest.get_active_version()
    click.echo(f"Current version: {active}")
    click.echo()

    # List available backups
    backups = backup.list_backups()
    if not backups:
        click.echo("No backups available for rollback")
        click.echo()
        click.echo("Backups are created automatically when upgrading.")
        sys.exit(1)

    click.echo("Available backups:")
    backup_versions = []
    for b in backups:
        if b["version"] == active:
            continue  # Skip current version
        suffix_str = f"-{b['suffix']}" if b['suffix'] else ""
        date_str = b["created_at"][:10] if b["created_at"] else "unknown"
        reason = b.get("reason", "unknown")
        click.echo(f"  {b['version']}{suffix_str} ({date_str}) - {reason}")
        if b["version"] not in backup_versions:
            backup_versions.append(b["version"])

    if not backup_versions:
        click.echo("  (no backups for other versions)")
        sys.exit(1)

    click.echo()

    # Determine version to rollback to
    if not version:
        # Default to most recent backup that isn't current version
        version = backup_versions[0] if backup_versions else None
        if not version:
            click.echo("No version to rollback to", err=True)
            sys.exit(1)

        version = click.prompt(f"Rollback to version", default=version)

    # Validate version has a backup
    backup_path = backup.get_backup_for_rollback(version)
    if not backup_path:
        click.echo(f"No backup found for version {version}", err=True)
        sys.exit(1)

    click.echo(f"Rollback to:     {version}")
    click.echo(f"Using backup:    {backup_path.name}")
    click.echo()
    click.echo("This will restore both code AND configuration.")
    click.echo()

    if not click.confirm("Proceed with rollback?"):
        click.echo("Rollback cancelled")
        return

    click.echo()
    click.echo("Rolling back...")
    click.echo()

    click.echo("[1/3] Stopping services...")
    run_syrvis_stop()

    click.echo("[2/3] Restoring from backup...")
    if not version_manager.rollback_to_backup(version):
        click.echo("Rollback failed", err=True)
        sys.exit(1)

    click.echo("[3/3] Rollback complete!")
    click.echo()
    click.echo(f"Rolled back to version {version}")
    click.echo()
    click.echo("Run 'syrvis start' to start services.")


def run_syrvis_stop():
    """Run 'syrvis stop' to stop services."""
    import subprocess
    import shutil

    # Find syrvis command
    syrvis_paths = [
        paths.get_syrvis_home() / "bin" / "syrvis",
        paths.get_syrvis_home() / "current" / "cli" / "venv" / "bin" / "syrvis",
    ]

    syrvis_cmd = None
    for p in syrvis_paths:
        try:
            if p.exists():
                syrvis_cmd = str(p)
                break
        except Exception:
            pass

    if not syrvis_cmd:
        syrvis_cmd = shutil.which("syrvis")

    if syrvis_cmd:
        try:
            subprocess.run([syrvis_cmd, "stop"], capture_output=True, timeout=60)
        except Exception:
            pass


@cli.command()
def check():
    """Check for available updates on GitHub."""
    click.echo()
    click.echo("Checking for updates...")
    click.echo()

    active = manifest.get_active_version()
    if active:
        click.echo(f"  Current version: {active}")
    else:
        click.echo("  Current version: (none installed)")

    release = downloader.get_latest_release()
    if not release:
        click.echo("  Could not fetch release information from GitHub")
        return

    latest = downloader.get_version_from_release(release)
    click.echo(f"  Latest version:  {latest}")
    click.echo()

    if not active:
        click.echo(f"  Run 'syrvisctl install' to install version {latest}")
        return

    cmp = downloader.compare_versions(active, latest)
    if cmp < 0:
        click.echo(f"  Update available: {active} -> {latest}")
        click.echo()

        # Show release notes
        body = release.get("body", "")
        if body:
            click.echo("  Release notes:")
            for line in body.split('\n')[:10]:
                click.echo(f"    {line}")
            click.echo()

        click.echo(f"  Run 'syrvisctl install {latest}' to update")
    elif cmp > 0:
        click.echo("  You are running a newer version than the latest release")
    else:
        click.echo("  You are running the latest version")


@cli.command()
def info():
    """Show installation information."""
    click.echo()
    click.echo("SyrvisCore Installation Info")
    click.echo("=" * 40)
    click.echo()

    # Manager info
    click.echo(f"Manager version: {__version__}")

    # Try to get installation info
    try:
        syrvis_home = paths.get_syrvis_home()
        click.echo(f"Install path:    {syrvis_home}")

        active = manifest.get_active_version()
        if active:
            click.echo(f"Active version:  {active}")
        else:
            click.echo("Active version:  (none)")

        setup_complete = manifest.verify_setup_complete()
        click.echo(f"Setup complete:  {'Yes' if setup_complete else 'No'}")

        versions = paths.list_installed_versions()
        click.echo(f"Versions:        {len(versions)} installed")

    except paths.SyrvisHomeError:
        click.echo("Install path:    (not installed)")
        click.echo()
        click.echo("Run 'syrvisctl install' to install a version")
        return

    # Show installed versions
    click.echo()
    click.echo("Installed versions:")
    for v in versions:
        marker = " (active)" if v == active else ""
        info = manifest.get_version_info(v)
        if info:
            installed = info.get("installed_at", "unknown")[:10]
            click.echo(f"  {v}{marker} - installed {installed}")
        else:
            click.echo(f"  {v}{marker}")

    # Show update history
    history = manifest.get_update_history()
    if history:
        click.echo()
        click.echo("Recent updates:")
        for entry in history[-5:]:
            from_v = entry.get("from", "?")
            to_v = entry.get("to", "?")
            update_type = entry.get("type", "update")
            timestamp = entry.get("timestamp", "")[:10]
            click.echo(f"  {timestamp}: {from_v} -> {to_v} ({update_type})")


@cli.command()
@click.option('--keep', default=2, help='Number of versions to keep')
@click.option('--dry-run', is_flag=True, help='Show what would be removed')
def cleanup(keep, dry_run):
    """Remove old versions to free disk space.

    Keeps the specified number of versions (default: 2).
    Never removes the currently active version.
    """
    click.echo()

    try:
        versions = paths.list_installed_versions()
        active = manifest.get_active_version()
    except paths.SyrvisHomeError:
        click.echo("No versions installed", err=True)
        return

    if len(versions) <= keep:
        click.echo(f"Only {len(versions)} version(s) installed, nothing to clean up")
        return

    # Get list of versions to remove
    to_remove = version_manager.cleanup_old_versions(keep, dry_run=True)

    if not to_remove:
        click.echo("No versions to remove")
        return

    click.echo(f"Versions to remove: {', '.join(to_remove)}")
    click.echo(f"Versions to keep:   {keep} (including active: {active})")
    click.echo()

    if dry_run:
        click.echo("Dry run - no changes made")
        return

    if not click.confirm("Proceed with cleanup?"):
        click.echo("Cleanup cancelled")
        return

    removed = version_manager.cleanup_old_versions(keep, dry_run=False)

    for v in removed:
        click.echo(f"  Removed: {v}")

    click.echo()
    click.echo("Cleanup complete")


@cli.command()
@click.option('--from-legacy', is_flag=True, help='Migrate from legacy (monolithic) installation')
@click.option('--dry-run', is_flag=True, help='Show what would be migrated')
def migrate(from_legacy, dry_run):
    """Migrate from a legacy installation.

    This command converts old (monolithic) installations to the new
    split-package architecture where the manager and service are separate.
    """
    click.echo()
    click.echo("SyrvisCore Migration")
    click.echo("=" * 40)
    click.echo()

    # Try to find legacy installation
    try:
        syrvis_home = paths.get_syrvis_home()
    except paths.SyrvisHomeError:
        click.echo("No existing installation found")
        click.echo("Run 'syrvisctl install' for a fresh installation")
        return

    # Check manifest schema version
    try:
        mf = manifest.get_manifest()
        schema_version = mf.get("schema_version", 1)
    except FileNotFoundError:
        click.echo("No manifest found - not a valid installation")
        sys.exit(1)

    if schema_version >= 3:
        click.echo("Installation is already using the new architecture")
        click.echo(f"Schema version: {schema_version}")
        return

    click.echo(f"Found legacy installation (schema v{schema_version})")
    click.echo(f"Install path: {syrvis_home}")
    click.echo()

    # Check for existing version directory
    current_link = syrvis_home / "current"
    if current_link.exists() and current_link.is_symlink():
        current_version = mf.get("active_version", "unknown")
        click.echo(f"Active version: {current_version}")

        # Check if venv exists in version directory
        version_venv = syrvis_home / "versions" / current_version / "cli" / "venv"
        if version_venv.exists():
            click.echo("Version structure already exists")

            if dry_run:
                click.echo()
                click.echo("Dry run - would update manifest to schema v3")
                return

            # Just update the manifest
            click.echo()
            click.echo("Updating manifest to schema v3...")
            mf["schema_version"] = 3
            manifest.save_manifest(mf)
            click.echo("Migration complete!")
            return

    click.echo()
    click.echo("Migration would:")
    click.echo("  1. Update manifest to schema v3")
    click.echo("  2. Preserve existing version directories")
    click.echo("  3. Keep config/ and data/ directories intact")
    click.echo()

    if dry_run:
        click.echo("Dry run - no changes made")
        return

    if not click.confirm("Proceed with migration?"):
        click.echo("Migration cancelled")
        return

    # Perform migration
    click.echo()
    click.echo("Migrating...")

    # Update manifest schema
    mf["schema_version"] = 3
    manifest.save_manifest(mf)
    click.echo("  Updated manifest schema")

    # Create syrvis wrapper if it doesn't exist
    paths.create_syrvis_wrapper()
    click.echo("  Created syrvis wrapper script")

    click.echo()
    click.echo("Migration complete!")
    click.echo()
    click.echo("Your existing installation has been migrated.")
    click.echo("You can now use 'syrvisctl' to manage versions.")


# =============================================================================
# Backup Commands
# =============================================================================

@cli.group('backup')
def backup_group():
    """Backup management commands."""
    pass


@backup_group.command('list')
def backup_list():
    """List available backups."""
    click.echo()
    click.echo("Available backups:")
    click.echo()

    backups = backup.list_backups()
    if not backups:
        click.echo("  No backups found")
        click.echo()
        click.echo("Backups are created automatically when upgrading,")
        click.echo("or manually with 'syrvisctl backup create'.")
        return

    click.echo(f"  {'Version':<12} {'Date':<12} {'Size':<10} {'Reason':<12}")
    click.echo(f"  {'-'*12} {'-'*12} {'-'*10} {'-'*12}")

    for b in backups:
        suffix_str = f"-{b['suffix']}" if b['suffix'] else ""
        version_str = f"{b['version']}{suffix_str}"
        date_str = b["created_at"][:10] if b["created_at"] else "unknown"
        size_mb = b["size"] / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB"
        reason = b.get("reason", "unknown")
        click.echo(f"  {version_str:<12} {date_str:<12} {size_str:<10} {reason:<12}")

    click.echo()
    try:
        click.echo(f"Location: {backup.get_backups_dir()}")
    except paths.SyrvisHomeError:
        pass


@backup_group.command('create')
@click.option('--output', '-o', type=click.Path(), help='Output path for backup file')
@click.option('--reason', type=click.Choice(['manual', 'post-setup']), default='manual',
              help='Reason for backup (affects naming)')
def backup_create(output, reason):
    """Create a manual backup of the current state.

    Use this to create a backup for off-NAS storage or before
    making manual configuration changes.
    """
    click.echo()
    click.echo("Creating backup...")
    click.echo()

    active = manifest.get_active_version()
    if not active:
        click.echo("No active version to backup", err=True)
        sys.exit(1)

    click.echo(f"  Version: {active}")

    try:
        if reason == "post-setup":
            # Post-setup backups use -N suffix
            backup_path = backup.create_post_setup_backup(active)
        else:
            # Manual backups go to specified output or default location
            output_path = Path(output) if output else None
            backup_path = backup.create_backup(
                output_path=output_path,
                version=active,
                reason="manual",
            )
        click.echo(f"  Output:  {backup_path}")
        click.echo()

        size_mb = backup_path.stat().st_size / (1024 * 1024)
        click.echo(f"Backup complete: {backup_path.name} ({size_mb:.1f} MB)")

    except Exception as e:
        click.echo(f"Backup failed: {e}", err=True)
        sys.exit(1)


@backup_group.command('cleanup')
@click.option('--keep', default=3, help='Number of versions to keep backups for')
@click.option('--dry-run', is_flag=True, help='Show what would be deleted')
def backup_cleanup(keep, dry_run):
    """Remove old backups to free disk space.

    Keeps all backups for the N most recent versions (default: 3).
    """
    click.echo()

    to_delete = backup.cleanup_old_backups(keep_versions=keep, dry_run=True)

    if not to_delete:
        click.echo(f"No backups to remove (keeping {keep} versions)")
        return

    click.echo(f"Backups to remove ({len(to_delete)}):")
    for path in to_delete:
        click.echo(f"  {path.name}")

    if dry_run:
        click.echo()
        click.echo("Dry run - no changes made")
        return

    click.echo()
    if not click.confirm("Proceed with cleanup?"):
        click.echo("Cleanup cancelled")
        return

    deleted = backup.cleanup_old_backups(keep_versions=keep, dry_run=False)
    click.echo()
    click.echo(f"Removed {len(deleted)} backup(s)")


@cli.command()
@click.argument('backup_file', required=False, type=click.Path(exists=True))
@click.option('--path', type=click.Path(), help='Installation path')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation')
def restore(backup_file, path, yes):
    """Restore from a backup archive.

    Use this for disaster recovery after a fresh DSM install.
    Restores configuration, data, and installs the version from backup.

    If BACKUP_FILE is not specified, shows available backups to choose from.
    """
    click.echo()
    click.echo("SyrvisCore Restore")
    click.echo("=" * 40)
    click.echo()

    # If no backup file specified, try to list available backups
    if not backup_file:
        try:
            backups = backup.list_backups()
            if backups:
                click.echo("Available backups:")
                click.echo()
                for i, b in enumerate(backups, 1):
                    suffix_str = f"-{b['suffix']}" if b['suffix'] else ""
                    date_str = b["created_at"][:10] if b["created_at"] else "unknown"
                    click.echo(f"  {i}. {b['version']}{suffix_str} ({date_str}) - {b['path']}")
                click.echo()

                choice = click.prompt("Select backup (number)", type=int, default=1)
                if 1 <= choice <= len(backups):
                    backup_file = str(backups[choice - 1]["path"])
                else:
                    click.echo("Invalid selection", err=True)
                    sys.exit(1)
            else:
                click.echo("No backups found in default location")
                click.echo()
                click.echo("Specify a backup file path:")
                click.echo("  syrvisctl restore /path/to/backup.tar.gz")
                sys.exit(1)
        except paths.SyrvisHomeError:
            click.echo("No existing installation found")
            click.echo()
            click.echo("Specify a backup file path:")
            click.echo("  syrvisctl restore /path/to/backup.tar.gz")
            sys.exit(1)

    backup_path = Path(backup_file)
    click.echo(f"Backup file: {backup_path}")

    # Read backup metadata
    import tarfile
    import json

    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            meta_file = tar.extractfile("backup-metadata.json")
            if meta_file:
                metadata = json.loads(meta_file.read().decode())
            else:
                click.echo("Backup file missing metadata", err=True)
                sys.exit(1)
    except Exception as e:
        click.echo(f"Could not read backup: {e}", err=True)
        sys.exit(1)

    version = metadata.get("version", "unknown")
    created_at = metadata.get("created_at", "unknown")[:10]
    original_path = metadata.get("syrvis_home", "/volume1/syrviscore")

    click.echo(f"Version:     {version}")
    click.echo(f"Created:     {created_at}")
    click.echo(f"Original:    {original_path}")
    click.echo()

    # Determine install path
    if path:
        install_path = Path(path)
    else:
        install_path = Path(original_path)
        if not yes:
            user_path = click.prompt(f"Install path [{install_path}]",
                                     default=str(install_path), show_default=False)
            install_path = Path(user_path)

    click.echo(f"Restore to:  {install_path}")
    click.echo()

    if not yes:
        if not click.confirm("Proceed with restore?"):
            click.echo("Restore cancelled")
            return

    click.echo()
    click.echo("Restoring...")
    click.echo()

    try:
        click.echo("[1/3] Extracting backup...")
        backup.restore_from_backup(backup_path, install_path)

        click.echo("[2/3] Creating wrapper scripts...")
        import os
        os.environ["SYRVIS_HOME"] = str(install_path)
        paths.create_syrvis_wrapper()
        paths.create_syrvis_profile()

        click.echo("[3/3] Restore complete!")
        click.echo()
        click.echo(f"Restored version {version} to {install_path}")
        click.echo()
        click.echo("Next steps:")
        click.echo(f"  1. Source the profile: source {install_path}/syrvis.profile")
        click.echo("  2. Run diagnostics: syrvis doctor")
        click.echo("  3. Start services: syrvis start")

    except Exception as e:
        click.echo(f"Restore failed: {e}", err=True)
        sys.exit(1)


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
