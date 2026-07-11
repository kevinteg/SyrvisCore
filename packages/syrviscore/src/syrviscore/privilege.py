"""
Privilege handling utilities for SyrvisCore.

Provides self-elevation capabilities for commands that need root access.
"""

import os
import sys
import shutil
import click


def is_simulation_mode() -> bool:
    """Check if running in DSM simulation mode."""
    return os.environ.get("DSM_SIM_ACTIVE") == "1"


def is_root() -> bool:
    """Check if running as root (or in simulation mode)."""
    # In simulation mode, we pretend to be root
    if is_simulation_mode():
        return True
    return os.geteuid() == 0


def self_elevate(reason: str = "This operation requires elevated privileges.") -> None:
    """Re-execute the current command with sudo.

    sudo's default env_reset strips SYRVIS_HOME, and re-execing sys.argv[0]
    (the venv console script) bypasses the bin/syrvis wrapper that sets it —
    so a naive re-exec leaves the elevated process unable to find its home.
    We pass SYRVIS_HOME through explicitly, resolving it first if unset.

    Args:
        reason: Message to display before elevating
    """
    sudo_path = shutil.which("sudo")
    if not sudo_path:
        click.echo("Error: sudo not found. Re-run this command with sudo.", err=True)
        sys.exit(1)

    # Resolve SYRVIS_HOME now, while we still can, and forward it across the
    # privilege boundary.
    syrvis_home = os.environ.get("SYRVIS_HOME")
    if not syrvis_home:
        try:
            from . import paths

            syrvis_home = str(paths.get_syrvis_home())
        except Exception:
            syrvis_home = None

    click.echo(f"\n{reason}")
    click.echo("Re-running with sudo...")
    click.echo()

    args = [sudo_path]
    if syrvis_home:
        args.append(f"SYRVIS_HOME={syrvis_home}")
    args += [sys.executable] + sys.argv
    os.execv(sudo_path, args)


def ensure_elevated(reason: str = "This operation requires elevated privileges.") -> None:
    """Ensure we're running with elevated privileges, or self-elevate.

    Args:
        reason: Message to display if elevation is needed
    """
    if not is_root():
        self_elevate(reason)
