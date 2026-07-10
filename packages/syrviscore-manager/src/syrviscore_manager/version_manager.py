"""
Version manager for SyrvisCore.

Handles installation, activation, and rollback of service versions.

v2 rules:
- Library layer: no printing (progress via optional ``log`` callback), no
  prompts (decisions via optional ``confirm`` callback), typed exceptions.
- Staged installs: a new version is fully built and verified in a hidden
  staging directory before anything existing is touched. A failed download
  or pip install can never destroy a working version.
- All mutations hold the installation lock.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

from . import backup, downloader, manifest, paths
from .errors import ActiveVersionError, InstallError, IntegrityError, VersionNotFoundError
from .locking import hold_lock

LogCallback = Callable[[str], None]
ConfirmCallback = Callable[[str], bool]

SERVICE_WHEEL_RE = re.compile(r"^syrviscore-(\d+\.\d+\.\d+)-py3-none-any\.whl$")


def _noop_log(_message: str) -> None:
    return None


def version_from_wheel_filename(wheel_path: Path) -> str:
    """Infer the service version from a wheel filename.

    Raises:
        InstallError: If the filename is not a service wheel.
    """
    match = SERVICE_WHEEL_RE.match(wheel_path.name)
    if not match:
        raise InstallError(
            "{} is not a syrviscore service wheel "
            "(expected syrviscore-<version>-py3-none-any.whl)".format(wheel_path.name)
        )
    return match.group(1)


# =============================================================================
# Venv backend (module-level so tests can substitute a fake)
# =============================================================================


def _create_venv(venv_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise InstallError("Failed to create venv: {}".format(result.stderr.strip()))


def _pip_install_wheel(venv_path: Path, wheel_path: Path) -> None:
    pip_path = venv_path / "bin" / "pip"
    subprocess.run([str(pip_path), "install", "--upgrade", "pip", "--quiet"], capture_output=True)
    result = subprocess.run(
        [str(pip_path), "install", "--no-cache-dir", "--quiet", str(wheel_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError("pip install failed: {}".format(result.stderr.strip()))


# =============================================================================
# Permissions
# =============================================================================


def set_tree_readable(path: Path) -> None:
    """Recursively set directories to 755 and files to 644 (bin/* to 755)."""
    for item in [path] + list(path.rglob("*")):
        try:
            if item.is_dir():
                item.chmod(0o755)
            elif item.parent.name == "bin":
                item.chmod(0o755)
            else:
                item.chmod(0o644)
        except OSError:
            continue  # e.g. broken symlinks inside venvs


# =============================================================================
# Core operations
# =============================================================================


def _build_version_tree(
    home: Path,
    version: str,
    wheel_path: Path,
    config_path: Optional[Path],
    build_dir: Path,
    log: LogCallback,
) -> None:
    """Build a complete version tree (cli/venv + wheel cache + config) in build_dir."""
    cli_dir = build_dir / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "build").mkdir(exist_ok=True)

    # Cache the wheel inside the version tree (required for backup/restore)
    wheel_cache = build_dir / "wheel"
    wheel_cache.mkdir(exist_ok=True)
    shutil.copy(str(wheel_path), str(wheel_cache / wheel_path.name))

    if config_path and config_path.exists():
        shutil.copy(str(config_path), str(build_dir / "build" / "config.yaml"))

    venv_path = cli_dir / "venv"
    log("Creating virtual environment...")
    _create_venv(venv_path)

    log("Installing service package...")
    _pip_install_wheel(venv_path, wheel_cache / wheel_path.name)

    syrvis_bin = venv_path / "bin" / "syrvis"
    if not syrvis_bin.exists():
        raise InstallError(
            "syrvis command not found after install (wheel {})".format(wheel_path.name)
        )


def install_version(
    home: Path,
    version: str,
    wheel_path: Path,
    config_path: Optional[Path] = None,
    force: bool = False,
    log: LogCallback = _noop_log,
) -> None:
    """
    Install a service version from a wheel file (staged, then swapped in).

    The version is built and verified under ``versions/.staging-<version>``
    first; only after a fully successful build is any existing copy of the
    version removed and the staging tree moved into place.

    Raises:
        InstallError: On build failure, or if the version exists and not force.
    """
    version = paths.validate_version(version)
    if not wheel_path.exists():
        raise InstallError("Wheel file not found: {}".format(wheel_path))

    with hold_lock(home):
        paths.ensure_directory_structure(home)
        final_dir = paths.version_dir(home, version)
        if final_dir.exists() and not force:
            raise InstallError(
                "Version {} is already installed (use force to reinstall)".format(version)
            )

        staging_dir = paths.versions_dir(home) / ".staging-{}".format(version)
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir))

        try:
            _build_version_tree(home, version, wheel_path, config_path, staging_dir, log)
        except BaseException:
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            raise

        # Build is complete and verified — now (and only now) swap it in.
        if final_dir.exists():
            shutil.rmtree(str(final_dir))
        staging_dir.rename(final_dir)

        set_tree_readable(final_dir)
        manifest.add_version_to_manifest(home, version, "available")


def uninstall_version(home: Path, version: str) -> None:
    """
    Uninstall a service version.

    Raises:
        VersionNotFoundError: If the version is not installed.
        ActiveVersionError: If the version is currently active.
    """
    version = paths.validate_version(version)

    with hold_lock(home):
        active = manifest.get_active_version(home)
        if version == active:
            raise ActiveVersionError(
                "Cannot uninstall active version {}; "
                "activate another version first".format(version)
            )

        vdir = paths.version_dir(home, version)
        if not vdir.exists():
            raise VersionNotFoundError("Version {} is not installed".format(version))

        shutil.rmtree(str(vdir))
        manifest.remove_version_from_manifest(home, version)


def activate_version(home: Path, version: str) -> None:
    """
    Activate a service version (atomic symlink switch + wrapper + manifest).

    Raises:
        VersionNotFoundError: If the version is not installed or incomplete.
    """
    version = paths.validate_version(version)

    with hold_lock(home):
        vdir = paths.version_dir(home, version)
        if not vdir.exists():
            raise VersionNotFoundError("Version {} is not installed".format(version))
        if not (vdir / "cli" / "venv" / "bin" / "syrvis").exists():
            raise VersionNotFoundError(
                "Version {} is incomplete (no working venv); reinstall it".format(version)
            )

        paths.update_current_symlink(home, version)
        paths.create_syrvis_wrapper(home)
        paths.create_syrvis_profile(home)
        manifest.set_active_version(home, version)


def install_from_wheel(
    home: Path,
    wheel_path: Path,
    config_path: Optional[Path] = None,
    force: bool = False,
    activate: bool = True,
    log: LogCallback = _noop_log,
) -> Dict[str, str]:
    """
    Install (and by default activate) a service version from a local wheel.

    This is the dev-loop primitive: no network, no GitHub — the wheel on disk
    is the artifact. Version is inferred from the wheel filename.

    Returns:
        {"version": <version>}
    """
    version = version_from_wheel_filename(wheel_path)
    log("Installing {} from local wheel {}".format(version, wheel_path))
    install_version(home, version, wheel_path, config_path, force=force, log=log)
    if activate:
        activate_version(home, version)
        log("Activated: {}".format(version))
    return {"version": version}


def download_and_install(
    home: Path,
    version: Optional[str] = None,
    force: bool = False,
    verify: bool = True,
    log: LogCallback = _noop_log,
    confirm_reinstall: Optional[ConfirmCallback] = None,
    progress: Optional[downloader.ProgressCallback] = None,
) -> Dict[str, object]:
    """
    Download and install a service version from GitHub.

    Order of operations (nothing existing is touched before the new artifact
    is downloaded and verified):
    1. Resolve the release and its wheel asset
    2. Download wheel (+ config.yaml, + SHA256SUMS) to a temp dir
    3. Verify the wheel checksum (unless ``verify=False``)
    4. Create a pre-upgrade backup of the current state
    5. Staged install, then activate

    Returns:
        {"version": str, "installed": bool, "skipped": bool}

    Raises:
        ReleaseNotFoundError / NetworkError / IntegrityError / InstallError
    """
    log("[1/5] Fetching release information...")
    if version:
        version = paths.validate_version(version)
        release = downloader.get_release_by_tag(version)
    else:
        release = downloader.get_latest_release()
        version = downloader.get_version_from_release(release)
        version = paths.validate_version(version)
    log("      Version: {}".format(version))

    # Early exit if already installed (before any download)
    already_installed = False
    try:
        already_installed = paths.version_dir(home, version).exists()
    except Exception:
        already_installed = False
    if already_installed and not force:
        if confirm_reinstall is None or not confirm_reinstall(
            "Version {} already installed. Reinstall?".format(version)
        ):
            log("      Version {} already installed — skipping".format(version))
            return {"version": version, "installed": False, "skipped": True}
        force = True

    wheel_asset = downloader.find_wheel_asset(release)
    if not wheel_asset:
        raise InstallError(
            "No service wheel (syrviscore-*.whl) found in release v{}".format(version)
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        log("[2/5] Downloading {}...".format(wheel_asset["name"]))
        wheel_path = tmp_path / wheel_asset["name"]
        downloader.download_file(wheel_asset["browser_download_url"], wheel_path, progress)

        config_path = None
        config_asset = downloader.find_config_asset(release)
        if config_asset:
            log("      Downloading config.yaml...")
            config_path = tmp_path / "config.yaml"
            downloader.download_file(config_asset["browser_download_url"], config_path)

        log("[3/5] Verifying integrity...")
        checksums_asset = downloader.find_checksums_asset(release)
        if checksums_asset:
            sums_path = tmp_path / checksums_asset["name"]
            downloader.download_file(checksums_asset["browser_download_url"], sums_path)
            sums = downloader.parse_sha256sums(sums_path.read_text())
            downloader.verify_asset_checksum(wheel_path, sums)
            log("      Checksum OK")
        elif verify:
            raise IntegrityError(
                "Release v{} has no SHA256SUMS asset. "
                "Re-run with --no-verify to install it anyway.".format(version)
            )
        else:
            log("      WARNING: no checksums published; installing unverified (--no-verify)")

        current_version = manifest.get_active_version(home) if paths.is_installation(home) else None
        if current_version and current_version != version:
            log("[4/5] Backing up current state ({})...".format(current_version))
            try:
                backup_path = backup.create_pre_upgrade_backup(home, current_version, version)
                if backup_path:
                    log("      Backup: {}".format(backup_path))
                else:
                    log("      Backup already exists for {}".format(current_version))
            except Exception as e:
                # Backup failure shouldn't block an upgrade, but must be visible
                log("      WARNING: could not create backup: {}".format(e))
        else:
            log("[4/5] No existing version to back up")

        log("[5/5] Installing...")
        install_version(home, version, wheel_path, config_path, force=force, log=log)

    activate_version(home, version)
    log("      Activated: {}".format(version))

    return {"version": version, "installed": True, "skipped": False}


def rollback_to_backup(home: Path, version: str, log: LogCallback = _noop_log) -> None:
    """
    Perform a full rollback to a version using its backup.

    A safety backup of the *current* state is taken first, so a bad rollback
    is itself recoverable.

    Raises:
        RestoreError / BackupError / VersionNotFoundError
    """
    version = paths.validate_version(version)

    backup_path = backup.get_backup_for_rollback(home, version)
    if not backup_path:
        raise VersionNotFoundError("No backup found for version {}".format(version))

    current = manifest.get_active_version(home)
    if current:
        log("Creating safety backup of current state ({})...".format(current))
        safety = backup.create_backup(
            home,
            version=current,
            reason="pre-rollback",
            suffix=backup.get_next_suffix(home, current),
        )
        log("Safety backup: {}".format(safety))

    log("Restoring from {}...".format(backup_path.name))
    backup.restore_from_backup(backup_path, home, log=log)


def cleanup_old_versions(home: Path, keep: int = 2, dry_run: bool = False) -> list:
    """
    Remove old versions, keeping the newest ``keep`` versions.

    The active version is always kept (and counts toward ``keep``).

    Returns:
        List of versions that were (or would be) removed
    """
    versions = paths.list_installed_versions(home)  # newest first
    active = manifest.get_active_version(home)

    # The active version is always kept and counts toward `keep`;
    # remaining slots go to the newest other versions.
    kept = [v for v in versions if v == active]
    to_remove = []
    for v in versions:
        if v == active:
            continue
        if len(kept) < keep:
            kept.append(v)
        else:
            to_remove.append(v)

    if dry_run:
        return to_remove

    removed = []
    for v in to_remove:
        uninstall_version(home, v)
        removed.append(v)

    return removed
