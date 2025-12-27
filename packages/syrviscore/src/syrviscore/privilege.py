"""
Privilege handling utilities for SyrvisCore.

Provides self-elevation capabilities for commands that need root access.
"""

import os
import sys
import shutil
import click


def is_root() -> bool:
    """Check if running as root."""
    return os.geteuid() == 0


def can_access_docker() -> bool:
    """Check if the current user can access Docker."""
    docker_socket = "/var/run/docker.sock"
    return os.path.exists(docker_socket) and os.access(docker_socket, os.R_OK | os.W_OK)


def needs_elevation_for_path(path) -> bool:
    """Check if we need elevation to write to a path."""
    from pathlib import Path
    path = Path(path)

    # Check if path exists and is writable
    if path.exists():
        return not os.access(path, os.W_OK)

    # Check parent directory
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent

    if parent.exists():
        return not os.access(parent, os.W_OK)

    return True  # Assume we need elevation if we can't determine


def self_elevate(reason: str = "This operation requires elevated privileges.") -> None:
    """Re-execute the current command with sudo.

    Args:
        reason: Message to display before elevating
    """
    sudo_path = shutil.which("sudo")
    if not sudo_path:
        click.echo("Error: sudo not found", err=True)
        sys.exit(1)

    click.echo(f"\n{reason}")
    click.echo("Re-running with sudo...")
    click.echo()

    # Re-execute with sudo, preserving arguments
    args = [sudo_path, sys.executable] + sys.argv
    os.execv(sudo_path, args)


def ensure_elevated(reason: str = "This operation requires elevated privileges.") -> None:
    """Ensure we're running with elevated privileges, or self-elevate.

    Args:
        reason: Message to display if elevation is needed
    """
    if not is_root():
        self_elevate(reason)


def ensure_docker_access() -> None:
    """Ensure we can access Docker, elevating if necessary."""
    if not can_access_docker():
        if not is_root():
            self_elevate("Docker socket is not accessible.")
