"""
SyrvisCore Manager CLI - syrvisctl command.

Provides version management for SyrvisCore service packages.

Commands:
    install [version]   - Download and install a service version (or --wheel)
    uninstall <version> - Remove a service version
    list                - List installed versions
    activate <version>  - Switch active version
    rollback [version]  - Rollback to previous version (full restore)
    check               - Check for updates
    info                - Show installation info
    cleanup             - Remove old versions
    backup              - Backup management commands
    restore             - Restore from backup

This module is a thin presentation shell: all real work happens in the
library modules (version_manager, backup, ...), which raise typed
SyrvisError exceptions and never print. Every read command supports
``--json``; every prompt is bypassable with ``-y``.
"""

import functools
import json as jsonlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from . import backup, downloader, manifest, paths, version_manager
from .__version__ import __version__
from .errors import SyrvisError


@click.group()
@click.version_option(version=__version__, prog_name="syrvisctl")
def cli():
    """SyrvisCore Manager - Version management for SyrvisCore."""
    pass


def handle_errors(f):
    """Render SyrvisError cleanly at the CLI boundary."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except SyrvisError as e:
            click.echo("Error: {}".format(e), err=True)
            sys.exit(e.exit_code)

    return wrapper


def emit_json(data) -> None:
    click.echo(jsonlib.dumps(data, indent=2, default=str))


# =============================================================================
# Privilege handling
# =============================================================================


def check_sudo_needed(path: Path) -> bool:
    """Check if we need sudo to write to the given path."""
    if path.exists():
        return not os.access(str(path), os.W_OK)

    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent

    if parent.exists():
        return not os.access(str(parent), os.W_OK)

    return True  # Assume we need sudo if we can't determine


def reexec_with_sudo(extra_args=None):
    """Re-execute the current command with sudo.

    All decisions made so far (install path, flags) must already be encoded
    in argv/extra_args — the elevated process starts from scratch.
    SYRVIS_HOME is passed through explicitly (sudo env_reset would drop it).
    """
    sudo_path = shutil.which("sudo")
    if not sudo_path:
        click.echo("Error: sudo not found. Re-run this command as root.", err=True)
        sys.exit(1)

    args = [sudo_path]
    syrvis_home = os.environ.get("SYRVIS_HOME")
    if syrvis_home:
        args.append("SYRVIS_HOME={}".format(syrvis_home))
    args += [sys.executable] + sys.argv + list(extra_args or [])

    click.echo("  Elevated privileges required. Re-running with sudo...")
    click.echo()
    os.execv(sudo_path, args)


def ensure_privileges(path: Path, extra_args=None) -> None:
    """Re-exec with sudo if writing to ``path`` requires it."""
    if check_sudo_needed(path) and os.geteuid() != 0:
        reexec_with_sudo(extra_args)


# =============================================================================
# Shared helpers
# =============================================================================


def _find_syrvis_command(home: Optional[Path]) -> Optional[str]:
    """Find the syrvis command path (wrapper, venv, or PATH)."""
    if home is not None:
        for p in (home / "bin" / "syrvis", home / "current" / "cli" / "venv" / "bin" / "syrvis"):
            if p.exists():
                return str(p)
    return shutil.which("syrvis")


def _run_syrvis(home: Optional[Path], *args, timeout: int = 60):
    """Run a syrvis subcommand, returning (ok, output)."""
    syrvis_cmd = _find_syrvis_command(home)
    if not syrvis_cmd:
        return False, "syrvis command not found"
    try:
        result = subprocess.run(
            [syrvis_cmd] + list(args), capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "syrvis {} timed out".format(" ".join(args))
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def _progress_bar(downloaded: int, total: int) -> None:
    if total <= 0:
        return
    percent = (downloaded / total) * 100
    bar_len = 30
    filled = int(bar_len * downloaded / total)
    bar = "=" * filled + "-" * (bar_len - filled)
    click.echo("\r      [{}] {:.0f}%".format(bar, percent), nl=False)
    if downloaded >= total:
        click.echo()


# =============================================================================
# Commands
# =============================================================================


@cli.command()
@click.argument("version", required=False)
@click.option(
    "--wheel",
    "wheel_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Install from a local wheel file instead of GitHub (dev loop)",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Bundle this config.yaml into the version (only with --wheel)",
)
@click.option("--force", is_flag=True, help="Force reinstall even if version exists")
@click.option("--clean", is_flag=True, help="Clean Docker containers/networks before reinstall")
@click.option("--path", type=click.Path(), help="Installation path (default: auto-detect)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option(
    "--no-verify",
    is_flag=True,
    help="Allow installing releases that publish no SHA256SUMS asset",
)
@click.option(
    "--no-backup",
    is_flag=True,
    help="Proceed even if the pre-upgrade backup fails (loses the rollback point)",
)
@handle_errors
def install(version, wheel_file, config_file, force, clean, path, yes, no_verify, no_backup):
    """Download and install a service version from GitHub.

    If VERSION is not specified, installs the latest release.
    With --wheel, installs the given local wheel file instead (no network).
    """
    click.echo()
    click.echo("Installing SyrvisCore service...")
    click.echo()

    # Determine installation path BEFORE any elevation, so the decision
    # survives the re-exec (encoded as --path).
    if path:
        install_path = Path(path)
    else:
        try:
            install_path = paths.resolve_home()
            click.echo("  Using existing installation: {}".format(install_path))
        except SyrvisError:
            default_path = paths.get_default_install_path()
            if yes:
                install_path = default_path
            else:
                user_path = click.prompt(
                    "  Installation path [{}]".format(default_path),
                    default=str(default_path),
                    show_default=False,
                )
                install_path = Path(user_path)
            click.echo("  Installing to: {}".format(install_path))

    extra_args = [] if path else ["--path", str(install_path)]
    ensure_privileges(install_path, extra_args)

    if clean:
        click.echo("[0/5] Cleaning Docker resources...")
        ok, output = _run_syrvis(install_path, "clean", "-y")
        if ok:
            click.echo("      Containers and networks removed")
        else:
            click.echo("      Warning: Clean failed - {}".format(output.strip()), err=True)
            click.echo("      Continuing with install...")
        click.echo()

    home = paths.resolve_home(explicit=install_path, create=True)

    if wheel_file:
        version_manager.install_from_wheel(
            home,
            Path(wheel_file),
            config_path=Path(config_file) if config_file else None,
            force=force,
            log=click.echo,
        )
    else:
        confirm = None if yes else (lambda msg: click.confirm("      " + msg, default=False))
        result = version_manager.download_and_install(
            home,
            version=version,
            force=force,
            verify=not no_verify,
            allow_backup_failure=no_backup,
            log=click.echo,
            confirm_reinstall=confirm,
            progress=_progress_bar,
        )
        if result["skipped"]:
            return

    click.echo()
    click.echo("Installation complete!")
    click.echo()

    profile_path = paths.get_syrvis_profile_path(home)
    if profile_path.exists():
        click.echo("To add 'syrvis' to your PATH:")
        click.echo("  source {}".format(profile_path))
        click.echo()

    click.echo("Next steps:")
    click.echo("  1. Run 'syrvis setup' to configure the service")
    click.echo("  2. Run 'syrvis start' to start the services")


@cli.command()
@click.argument("version")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@handle_errors
def uninstall(version, yes):
    """Remove a service version.

    Cannot uninstall the currently active version.
    """
    click.echo()
    home = paths.resolve_home()
    version = paths.validate_version(version)

    version_dir = paths.version_dir(home, version)
    if not version_dir.exists():
        click.echo("Version {} is not installed".format(version), err=True)
        sys.exit(1)

    active = manifest.get_active_version(home)
    if version == active:
        click.echo("Cannot uninstall active version: {}".format(version), err=True)
        click.echo("Use 'syrvisctl activate <other-version>' first", err=True)
        sys.exit(1)

    ensure_privileges(version_dir)

    if not yes:
        if not click.confirm("Uninstall version {}?".format(version)):
            click.echo("Uninstall cancelled")
            return

    click.echo("Uninstalling {}...".format(version))
    version_manager.uninstall_version(home, version)
    click.echo("Version {} uninstalled".format(version))


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def list_versions(as_json):
    """List installed service versions."""
    try:
        home = paths.resolve_home()
        versions = paths.list_installed_versions(home)
        active = manifest.get_active_version(home)
    except SyrvisError:
        versions, active = [], None

    if as_json:
        emit_json({"versions": versions, "active": active})
        return

    click.echo()
    click.echo("Installed versions:")
    click.echo()

    if not versions:
        click.echo("  No versions installed")
        click.echo()
        click.echo("Run 'syrvisctl install' to install a version")
        return

    for v in versions:
        marker = " (active)" if v == active else ""
        click.echo("  {}{}".format(v, marker))
    click.echo()


@cli.command()
@click.argument("version")
@handle_errors
def activate(version):
    """Activate a specific service version.

    Switches the 'current' symlink to point to the specified version.
    """
    click.echo()
    home = paths.resolve_home()
    version = paths.validate_version(version)

    version_dir = paths.version_dir(home, version)
    if not version_dir.exists():
        click.echo("Version {} is not installed".format(version), err=True)
        click.echo()
        click.echo("Installed versions:")
        for v in paths.list_installed_versions(home):
            click.echo("  {}".format(v))
        sys.exit(1)

    active = manifest.get_active_version(home)
    if version == active:
        click.echo("Version {} is already active".format(version))
        return

    ensure_privileges(paths.current_symlink(home))

    click.echo("Activating version {}...".format(version))
    version_manager.activate_version(home, version)
    click.echo("Activated: {}".format(version))
    click.echo()
    click.echo("You may need to restart services:")
    click.echo("  syrvis restart")


@cli.command()
@click.argument("version", required=False)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@handle_errors
def rollback(version, yes):
    """Rollback to a previous version (full restore from backup).

    Restores both code AND configuration from the backup archive.
    If VERSION is not specified, uses the most recent backed-up version.
    """
    home = paths.resolve_home()
    ensure_privileges(home)

    click.echo()
    click.echo("SyrvisCore Rollback")
    click.echo("=" * 40)
    click.echo()

    active = manifest.get_active_version(home)
    click.echo("Current version: {}".format(active or "(none)"))
    click.echo()

    backups = backup.list_backups(home)
    backup_versions = []
    for b in backups:
        if b["version"] != active and b["version"] not in backup_versions:
            backup_versions.append(b["version"])

    if not backup_versions:
        click.echo("No backups available for rollback")
        click.echo()
        click.echo("Backups are created automatically when upgrading.")
        sys.exit(1)

    click.echo("Available backups:")
    for b in backups:
        if b["version"] == active:
            continue
        suffix_str = "-{}".format(b["suffix"]) if b["suffix"] else ""
        date_str = b["created_at"][:10] if b["created_at"] else "unknown"
        click.echo("  {}{} ({}) - {}".format(b["version"], suffix_str, date_str, b.get("reason")))
    click.echo()

    if not version:
        version = backup_versions[0]
        if not yes:
            version = click.prompt("Rollback to version", default=version)
    version = paths.validate_version(version)

    backup_path = backup.get_backup_for_rollback(home, version)
    if not backup_path:
        click.echo("No backup found for version {}".format(version), err=True)
        sys.exit(1)

    click.echo("Rollback to:     {}".format(version))
    click.echo("Using backup:    {}".format(backup_path.name))
    click.echo()
    click.echo("This will restore both code AND configuration.")
    click.echo()

    if not yes and not click.confirm("Proceed with rollback?"):
        click.echo("Rollback cancelled")
        return

    click.echo()
    click.echo("[1/3] Stopping services...")
    ok, output = _run_syrvis(home, "stop")
    if not ok:
        click.echo("      Warning: could not stop services: {}".format(output.strip()), err=True)

    click.echo("[2/3] Restoring from backup...")
    version_manager.rollback_to_backup(home, version, log=lambda m: click.echo("      " + m))

    click.echo("[3/3] Rollback complete!")
    click.echo()
    click.echo("Rolled back to version {}".format(version))
    click.echo()
    click.echo("Run 'syrvis start' to start services.")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def check(as_json):
    """Check for available updates on GitHub."""
    try:
        home = paths.resolve_home()
        active = manifest.get_active_version(home)
    except SyrvisError:
        active = None

    release = downloader.get_latest_release()
    latest = downloader.get_version_from_release(release)
    update_available = bool(active) and downloader.compare_versions(active, latest) < 0

    if as_json:
        emit_json(
            {
                "current": active,
                "latest": latest,
                "update_available": update_available,
                "release_notes": release.get("body", ""),
            }
        )
        return

    click.echo()
    click.echo("Checking for updates...")
    click.echo()
    click.echo("  Current version: {}".format(active or "(none installed)"))
    click.echo("  Latest version:  {}".format(latest))
    click.echo()

    if not active:
        click.echo("  Run 'syrvisctl install' to install version {}".format(latest))
        return

    if update_available:
        click.echo("  Update available: {} -> {}".format(active, latest))
        click.echo()
        body = release.get("body", "")
        if body:
            click.echo("  Release notes:")
            for line in body.split("\n")[:10]:
                click.echo("    {}".format(line))
            click.echo()
        click.echo("  Run 'syrvisctl install {}' to update".format(latest))
    elif downloader.compare_versions(active, latest) > 0:
        click.echo("  You are running a newer version than the latest release")
    else:
        click.echo("  You are running the latest version")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def info(as_json):
    """Show installation information."""
    try:
        home = paths.resolve_home()
    except SyrvisError:
        if as_json:
            emit_json({"manager_version": __version__, "installed": False})
            return
        click.echo()
        click.echo("SyrvisCore Installation Info")
        click.echo("=" * 40)
        click.echo()
        click.echo("Manager version: {}".format(__version__))
        click.echo("Install path:    (not installed)")
        click.echo()
        click.echo("Run 'syrvisctl install' to install a version")
        return

    active = manifest.get_active_version(home)
    versions = paths.list_installed_versions(home)
    setup_complete = manifest.verify_setup_complete(home)
    history = manifest.get_update_history(home)

    if as_json:
        emit_json(
            {
                "manager_version": __version__,
                "installed": True,
                "home": str(home),
                "active": active,
                "setup_complete": setup_complete,
                "versions": {v: manifest.get_version_info(home, v) or {} for v in versions},
                "update_history": history[-5:],
            }
        )
        return

    click.echo()
    click.echo("SyrvisCore Installation Info")
    click.echo("=" * 40)
    click.echo()
    click.echo("Manager version: {}".format(__version__))
    click.echo("Install path:    {}".format(home))
    click.echo("Active version:  {}".format(active or "(none)"))
    click.echo("Setup complete:  {}".format("Yes" if setup_complete else "No"))
    click.echo("Versions:        {} installed".format(len(versions)))

    click.echo()
    click.echo("Installed versions:")
    for v in versions:
        marker = " (active)" if v == active else ""
        vinfo = manifest.get_version_info(home, v)
        if vinfo:
            installed = vinfo.get("installed_at", "unknown")[:10]
            click.echo("  {}{} - installed {}".format(v, marker, installed))
        else:
            click.echo("  {}{}".format(v, marker))

    if history:
        click.echo()
        click.echo("Recent updates:")
        for entry in history[-5:]:
            click.echo(
                "  {}: {} -> {} ({})".format(
                    entry.get("timestamp", "")[:10],
                    entry.get("from", "?"),
                    entry.get("to", "?"),
                    entry.get("type", "update"),
                )
            )


@cli.command()
@click.option("--keep", default=2, help="Number of versions to keep")
@click.option("--dry-run", is_flag=True, help="Show what would be removed")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def cleanup(keep, dry_run, yes):
    """Remove old versions to free disk space.

    Keeps the specified number of versions (default: 2).
    Never removes the currently active version.
    """
    click.echo()
    home = paths.resolve_home()

    versions = paths.list_installed_versions(home)
    active = manifest.get_active_version(home)

    to_remove = version_manager.cleanup_old_versions(home, keep, dry_run=True)
    if not to_remove:
        click.echo("Only {} version(s) installed, nothing to clean up".format(len(versions)))
        return

    click.echo("Versions to remove: {}".format(", ".join(to_remove)))
    click.echo("Versions to keep:   {} (including active: {})".format(keep, active))
    click.echo()

    if dry_run:
        click.echo("Dry run - no changes made")
        return

    ensure_privileges(paths.versions_dir(home))

    if not yes and not click.confirm("Proceed with cleanup?"):
        click.echo("Cleanup cancelled")
        return

    removed = version_manager.cleanup_old_versions(home, keep, dry_run=False)
    for v in removed:
        click.echo("  Removed: {}".format(v))
    click.echo()
    click.echo("Cleanup complete")


# =============================================================================
# Backup Commands
# =============================================================================


@cli.group("backup")
def backup_group():
    """Backup management commands."""
    pass


@backup_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def backup_list(as_json):
    """List available backups."""
    home = paths.resolve_home()
    backups = backup.list_backups(home)

    if as_json:
        emit_json(
            {
                "backups": [
                    {
                        "filename": b["filename"],
                        "version": b["version"],
                        "suffix": b["suffix"],
                        "size": b["size"],
                        "created_at": b["created_at"],
                        "reason": b["reason"],
                        "path": str(b["path"]),
                    }
                    for b in backups
                ]
            }
        )
        return

    click.echo()
    click.echo("Available backups:")
    click.echo()

    if not backups:
        click.echo("  No backups found")
        click.echo()
        click.echo("Backups are created automatically when upgrading,")
        click.echo("or manually with 'syrvisctl backup create'.")
        return

    click.echo("  {:<12} {:<12} {:<10} {:<12}".format("Version", "Date", "Size", "Reason"))
    click.echo("  {} {} {} {}".format("-" * 12, "-" * 12, "-" * 10, "-" * 12))

    for b in backups:
        suffix_str = "-{}".format(b["suffix"]) if b["suffix"] else ""
        version_str = "{}{}".format(b["version"], suffix_str)
        date_str = b["created_at"][:10] if b["created_at"] else "unknown"
        size_str = "{:.1f} MB".format(b["size"] / (1024 * 1024))
        click.echo(
            "  {:<12} {:<12} {:<10} {:<12}".format(
                version_str, date_str, size_str, b.get("reason") or "unknown"
            )
        )

    click.echo()
    click.echo("Location: {}".format(backup.get_backups_dir(home)))


@backup_group.command("create")
@click.option("--output", "-o", type=click.Path(), help="Output path for backup file")
@click.option(
    "--reason",
    type=click.Choice(["manual", "post-setup"]),
    default="manual",
    help="Reason for backup (affects naming)",
)
@handle_errors
def backup_create(output, reason):
    """Create a manual backup of the current state.

    Use this to create a backup for off-NAS storage or before
    making manual configuration changes.
    """
    click.echo()
    click.echo("Creating backup...")
    click.echo()

    home = paths.resolve_home()
    active = manifest.get_active_version(home)
    if not active:
        click.echo("No active version to backup", err=True)
        sys.exit(1)

    click.echo("  Version: {}".format(active))

    if reason == "post-setup":
        backup_path = backup.create_post_setup_backup(home, active)
    else:
        backup_path = backup.create_backup(
            home,
            output_path=Path(output) if output else None,
            version=active,
            reason="manual",
        )

    click.echo("  Output:  {}".format(backup_path))
    click.echo()

    size_mb = backup_path.stat().st_size / (1024 * 1024)
    click.echo("Backup complete: {} ({:.1f} MB)".format(backup_path.name, size_mb))


@backup_group.command("cleanup")
@click.option("--keep", default=3, help="Number of versions to keep backups for")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def backup_cleanup(keep, dry_run, yes):
    """Remove old backups to free disk space.

    Keeps all backups for the N most recent versions (default: 3).
    """
    click.echo()
    home = paths.resolve_home()

    to_delete = backup.cleanup_old_backups(home, keep_versions=keep, dry_run=True)
    if not to_delete:
        click.echo("No backups to remove (keeping {} versions)".format(keep))
        return

    click.echo("Backups to remove ({}):".format(len(to_delete)))
    for path in to_delete:
        click.echo("  {}".format(path.name))

    if dry_run:
        click.echo()
        click.echo("Dry run - no changes made")
        return

    click.echo()
    if not yes and not click.confirm("Proceed with cleanup?"):
        click.echo("Cleanup cancelled")
        return

    deleted = backup.cleanup_old_backups(home, keep_versions=keep, dry_run=False)
    click.echo()
    click.echo("Removed {} backup(s)".format(len(deleted)))


@cli.command()
@click.argument("backup_file", required=False, type=click.Path(exists=True))
@click.option("--path", type=click.Path(), help="Installation path")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
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

    if not backup_file:
        try:
            home = paths.resolve_home()
            backups = backup.list_backups(home)
        except SyrvisError:
            backups = []

        if not backups:
            click.echo("No backups found in default location")
            click.echo()
            click.echo("Specify a backup file path:")
            click.echo("  syrvisctl restore /path/to/backup.tar.gz")
            sys.exit(1)

        click.echo("Available backups:")
        click.echo()
        for i, b in enumerate(backups, 1):
            suffix_str = "-{}".format(b["suffix"]) if b["suffix"] else ""
            date_str = b["created_at"][:10] if b["created_at"] else "unknown"
            click.echo(
                "  {}. {}{} ({}) - {}".format(i, b["version"], suffix_str, date_str, b["path"])
            )
        click.echo()

        choice = 1 if yes else click.prompt("Select backup (number)", type=int, default=1)
        if not 1 <= choice <= len(backups):
            click.echo("Invalid selection", err=True)
            sys.exit(1)
        backup_file = str(backups[choice - 1]["path"])

    backup_path = Path(backup_file)
    click.echo("Backup file: {}".format(backup_path))

    metadata = backup.read_backup_metadata(backup_path)
    version = metadata.get("version", "unknown")
    created_at = (metadata.get("created_at") or "unknown")[:10]
    original_path = metadata.get("syrvis_home", "/volume1/syrviscore")

    click.echo("Version:     {}".format(version))
    click.echo("Created:     {}".format(created_at))
    click.echo("Original:    {}".format(original_path))
    click.echo()

    if path:
        install_path = Path(path)
    else:
        install_path = Path(original_path)
        if not yes:
            user_path = click.prompt(
                "Install path [{}]".format(install_path),
                default=str(install_path),
                show_default=False,
            )
            install_path = Path(user_path)

    click.echo("Restore to:  {}".format(install_path))
    click.echo()

    if not yes and not click.confirm("Proceed with restore?"):
        click.echo("Restore cancelled")
        return

    # Elevation happens only after every decision is encoded in flags
    extra_args = []
    if not path:
        extra_args += ["--path", str(install_path)]
    if not yes:
        extra_args += ["-y"]
    ensure_privileges(install_path, extra_args)

    click.echo()
    click.echo("Restoring...")
    click.echo()

    click.echo("[1/2] Extracting backup...")
    metadata = backup.restore_from_backup(
        backup_path, install_path, log=lambda m: click.echo("      " + m)
    )

    click.echo("[2/2] Restore complete!")
    click.echo()
    click.echo("Restored version {} to {}".format(version, install_path))

    l2 = metadata.get("layer2_services") or []
    if l2:
        click.echo()
        click.echo("Restored {} Layer-2 service(s): {}".format(len(l2), ", ".join(l2)))
        click.echo("  Start them after 'syrvis start' with: syrvis service start <name>")

    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Source the profile: source {}/syrvis.profile".format(install_path))
    click.echo("  2. Run diagnostics: syrvis doctor")
    click.echo("  3. Start services: syrvis start")
    click.echo()
    click.echo(
        "Note: docker-group membership, the macvlan shim, and the S99 boot hook are\n"
        "      NOT in the backup — they come from 'syrvis setup'. On a bare-metal\n"
        "      rebuild, run 'sudo syrvis setup' before/after restore so Traefik can\n"
        "      bind its IP and services survive reboot."
    )


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
