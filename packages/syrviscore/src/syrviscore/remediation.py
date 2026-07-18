"""
Sanctioned remediation dispatch — the single place that maps a validator's
``fix_action`` to the privileged operation that fixes it.

Both ``syrvis doctor --fix`` and ``syrvis verify --fix`` route through
``apply_fix`` here, so a fixer is wired up in exactly one place. The audit's H3
finding (doctor silently ignored ``boot_script``/``manifest_perms``) was caused
by that dispatch being duplicated and drifting; consolidating it here prevents
a recurrence.

These operations change system state (docker group, socket permissions, boot
hooks, symlinks) and therefore require root — callers must elevate first.
"""

from pathlib import Path
from typing import Optional, Tuple

from . import paths, privileged_ops


def resolve_install_dir() -> Optional[Path]:
    """Best-effort resolution of SYRVIS_HOME for fixers that need it."""
    try:
        return paths.get_syrvis_home()
    except Exception:
        return None


def apply_fix(fix_action: Optional[str], install_dir: Optional[Path]) -> Tuple[bool, str]:
    """Apply the privileged remediation for a single validator ``fix_action``.

    Returns (ok, message). Unknown or un-actionable actions return
    ``(False, ...)`` explicitly rather than being silently skipped.
    """
    action = fix_action

    if action == "docker_group":
        return privileged_ops.ensure_docker_group()

    if action and action.startswith("user_group:"):
        user = action.split(":", 1)[1]
        return privileged_ops.ensure_user_in_docker_group(user)

    if action == "socket_perms":
        return privileged_ops.ensure_docker_socket_permissions()

    if action == "symlink":
        if not install_dir:
            return False, "symlink fix needs the install directory"
        return privileged_ops.ensure_global_symlink(install_dir)

    if action and action.startswith("startup:"):
        if not install_dir:
            return False, "startup fix needs the install directory"
        user = action.split(":", 1)[1]
        return privileged_ops.ensure_startup_script(install_dir, user)

    if action == "boot_script":
        if not install_dir:
            return False, "boot_script fix needs the install directory"
        return privileged_ops.ensure_boot_script(install_dir)

    if action == "manifest_perms":
        return privileged_ops.ensure_manifest_permissions(install_dir)

    if action == "config_tree_perms":
        return privileged_ops.ensure_config_tree_readable(install_dir)

    if action == "schedule_block":
        # Re-apply SyrvisCore's managed /etc/crontab block from config/jobs.d
        # (DSM can drop it on a UI task edit). No-op with an empty jobs.d.
        return privileged_ops.ensure_schedule_block(install_dir)

    return False, "No automatic fix wired up for '{}'".format(action)
