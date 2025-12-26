"""
SyrvisCore Manager CLI - syrvisctl command.

Provides version management for SyrvisCore service packages.

Commands:
    install [version]   - Download and install a service version
    uninstall <version> - Remove a service version
    list                - List installed versions
    activate <version>  - Switch active version
    rollback            - Switch to previous version
    check               - Check for updates
    info                - Show installation info
    cleanup             - Remove old versions
"""

import sys
from pathlib import Path

import click

from .__version__ import __version__
from . import paths
from . import manifest
from . import downloader
from . import version_manager


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


@cli.command()
@click.argument('version', required=False)
@click.option('--force', is_flag=True, help='Force reinstall even if version exists')
@click.option('--path', type=click.Path(), help='Installation path (default: auto-detect)')
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompts')
def install(version, force, path, yes):
    """Download and install a service version from GitHub.

    If VERSION is not specified, installs the latest release.
    """
    import os

    click.echo()
    click.echo("Installing SyrvisCore service...")
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
def rollback():
    """Rollback to the previous version."""
    click.echo()

    active = manifest.get_active_version()
    previous = version_manager.get_previous_version()

    if not previous:
        click.echo("No previous version available for rollback")
        sys.exit(1)

    click.echo(f"Current version: {active}")
    click.echo(f"Rollback to:     {previous}")
    click.echo()

    if not click.confirm("Proceed with rollback?"):
        click.echo("Rollback cancelled")
        return

    click.echo()
    click.echo("Rolling back...")

    if version_manager.activate_version(previous):
        click.echo(f"Rolled back to version {previous}")
        click.echo()
        click.echo("You may need to restart services:")
        click.echo("  syrvis restart")
    else:
        click.echo("Rollback failed", err=True)
        sys.exit(1)


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


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
