"""Sync the operator-seam enforcement artifacts from the ACTIVE service version.

The sudoers policy + forced-command shim are GENERATED from the platform's verb
registry (``syrviscore.seam``, shipped inside every service version >= 0.4).
Provisioning installs them once and records the deployment's seam policy; from
then on ``syrvisctl activate`` re-renders both from the newly activated version
— new verbs arrive with the release that implements them, and a rollback
narrows the seam back with it. No manual re-provision per verb.

Trust tradeoff (chosen at provision time as ``auto_seam_update``): with auto
sync ON, installing + activating a release updates the enforcement boundary,
so the trust anchor becomes the release channel plus this root-held policy
file — not a human re-provision. Provision with ``--no-auto-seam-update`` to
keep the boundary pinned; ``syrvisctl seam sync`` then updates it on demand.

The manager never imports syrviscore (separate venvs) — it execs the active
version's venv python: ``<home>/current/cli/venv/bin/python -m
syrviscore.seam.gen``. Installs are atomic (temp + rename in the target dir),
and the sudoers content is validated with visudo when the system has one.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import SyrvisError

# Written by the provision script — must match syrviscore.seam.gen.
SEAM_POLICY_PATH = Path("/var/log/syrviscore-mcp-provision/seam-policy.json")


class SeamSyncError(SyrvisError):
    """The seam artifacts could not be regenerated or installed."""


def load_policy(policy_path: Path = SEAM_POLICY_PATH) -> Optional[Dict[str, Any]]:
    """Read the provision-time seam policy; None when never provisioned."""
    if not policy_path.exists():
        return None
    try:
        data = json.loads(policy_path.read_text())
    except (OSError, ValueError) as e:
        raise SeamSyncError("unreadable seam policy {}: {}".format(policy_path, e))
    if not isinstance(data, dict):
        raise SeamSyncError("seam policy {} is not a JSON object".format(policy_path))
    return data


def _venv_python(home: Path) -> Path:
    return home / "current" / "cli" / "venv" / "bin" / "python"


def _render(home: Path, policy: Dict[str, Any], what: str) -> str:
    """Render 'sudoers' or 'shim' via the active version's seam generator."""
    python = _venv_python(home)
    if not python.exists():
        raise SeamSyncError("no active version venv at {}".format(python))
    argv = [
        str(python),
        "-m",
        "syrviscore.seam.gen",
        what,
        "--home",
        str(home),
        "--operator",
        str(policy.get("operator", "syrvis-operator")),
        "--syrvisctl",
        str(policy.get("syrvisctl_path", "/var/packages/syrviscore/target/venv/bin/syrvisctl")),
        "--shim-path",
        str(policy.get("shim_path", "/usr/local/bin/syrvis-mcp-shim")),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "No module named" in stderr:
            raise SeamSyncError(
                "the active service version has no seam generator "
                "(syrviscore.seam needs service >= 0.4); artifacts left unchanged"
            )
        raise SeamSyncError("seam generator failed: {}".format(stderr or result.returncode))
    return result.stdout


def _read_or_none(path: Path) -> Optional[str]:
    """Read ``path``, or None if it is absent OR unreadable (e.g. the 0440
    root sudoers file compared by an unelevated ``seam status``/dry-run).

    Returning None means "can't confirm it matches" → treated as needing an
    update; the actual install re-checks under root and no-ops if identical.
    """
    try:
        return path.read_text()
    except OSError:
        return None


def _install(path: Path, content: str, mode: int, validate_sudoers: bool = False) -> bool:
    """Atomically install ``content`` at ``path``; returns False when unchanged.

    The temp file uses a DOTTED name in the target directory (sudo's
    #includedir ignores dotfiles), so sudo only ever sees the atomic rename —
    the same guarantee the provision script gives (DSM has no visudo).
    """
    if _read_or_none(path) == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".{}.".format(path.name))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        if validate_sudoers and shutil.which("visudo"):
            check = subprocess.run(
                ["visudo", "-cf", tmp_name], capture_output=True, text=True, timeout=30
            )
            if check.returncode != 0:
                raise SeamSyncError(
                    "generated sudoers failed visudo validation (not installed): {}".format(
                        (check.stderr or check.stdout or "").strip()
                    )
                )
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


def sync_seam(
    home: Path,
    policy: Dict[str, Any],
    sudoers_path: Path = Path("/etc/sudoers.d/syrviscore-mcp"),
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Regenerate + install the seam artifacts from the active version.

    Returns a report: {"sudoers": "updated"|"unchanged", "shim": ...}.
    """
    shim_path = Path(str(policy.get("shim_path", "/usr/local/bin/syrvis-mcp-shim")))
    sudoers_content = _render(home, policy, "sudoers")
    shim_content = _render(home, policy, "shim")

    if dry_run:
        # Tolerant read: a comparison from an unelevated `seam status` can't read
        # the 0440 root sudoers file — report "would update" (unknown) rather
        # than crashing on PermissionError (handle_errors only catches SyrvisError).
        return {
            "dry_run": True,
            "sudoers": (
                "unchanged" if _read_or_none(sudoers_path) == sudoers_content else "would update"
            ),
            "shim": ("unchanged" if _read_or_none(shim_path) == shim_content else "would update"),
        }

    changed_sudoers = _install(sudoers_path, sudoers_content, 0o440, validate_sudoers=True)
    changed_shim = _install(shim_path, shim_content, 0o755)
    return {
        "dry_run": False,
        "sudoers": "updated" if changed_sudoers else "unchanged",
        "shim": "updated" if changed_shim else "unchanged",
    }


def auto_sync_after_activate(home: Path) -> Optional[Dict[str, Any]]:
    """The `syrvisctl activate` hook: sync when provisioned with auto ON.

    Returns the sync report, or None when skipped (never provisioned, or
    auto_seam_update is off). Raises SeamSyncError on a real failure — the
    caller reports it WITHOUT failing the activation (the version switch
    already happened; a stale seam is recoverable via `syrvisctl seam sync`).
    """
    policy = load_policy()
    if policy is None:
        return None
    if not policy.get("auto_seam_update", False):
        return None
    return sync_seam(home, policy)
