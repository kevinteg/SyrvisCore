"""
Update command for SyrvisCore - handles version management and rollback.

This module provides:
- Checking for updates from GitHub releases
- Downloading new versions
- Installing updates with version preservation
- Rolling back to previous versions
- Listing installed versions
- Cleaning up old versions
"""

import click
import sys
import os
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import requests

from . import paths
from .__version__ import __version__


# GitHub repository for releases
GITHUB_REPO = "kevinteg/SyrvisCore"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# How many versions to keep by default
DEFAULT_VERSIONS_TO_KEEP = 2


def get_latest_release() -> Optional[Dict[str, Any]]:
    """Fetch latest release info from GitHub."""
    try:
        response = requests.get(
            f"{GITHUB_API_URL}/latest",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None


def get_release_by_tag(tag: str) -> Optional[Dict[str, Any]]:
    """Fetch specific release by tag."""
    try:
        response = requests.get(
            f"{GITHUB_API_URL}/tags/{tag}",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None


def list_releases(limit: int = 10) -> List[Dict[str, Any]]:
    """List available releases from GitHub."""
    try:
        response = requests.get(
            GITHUB_API_URL,
            params={"per_page": limit},
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse version string into comparable tuple."""
    # Remove 'v' prefix if present
    v = version_str.lstrip('v')
    try:
        return tuple(int(p) for p in v.split('.'))
    except ValueError:
        return (0, 0, 0)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings. Returns -1, 0, or 1."""
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    if t1 < t2:
        return -1
    elif t1 > t2:
        return 1
    return 0


def find_spk_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the SPK file in release assets."""
    for asset in release.get("assets", []):
        if asset["name"].endswith(".spk"):
            return asset
    return None


def download_file(url: str, dest: Path, show_progress: bool = True) -> bool:
    """Download file with optional progress display."""
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if show_progress and total_size > 0:
                        percent = (downloaded / total_size) * 100
                        bar_len = 30
                        filled = int(bar_len * downloaded / total_size)
                        bar = '=' * filled + '-' * (bar_len - filled)
                        click.echo(f"\r      [{bar}] {percent:.0f}%", nl=False)

        if show_progress:
            click.echo()  # Newline after progress bar

        return True
    except Exception as e:
        click.echo(f"\n      Error: {e}", err=True)
        return False


def extract_spk(spk_path: Path, dest_dir: Path) -> bool:
    """Extract SPK file (which is a tar.gz)."""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)

        # SPK is a tar file
        result = subprocess.run(
            ["tar", "-xf", str(spk_path), "-C", str(dest_dir)],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            click.echo(f"      Error extracting SPK: {result.stderr}", err=True)
            return False

        return True
    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def install_version(version: str, spk_path: Path) -> bool:
    """Install a version from SPK file."""
    try:
        syrvis_home = paths.get_syrvis_home()
        version_dir = paths.get_version_dir(version)

        # Create version directory structure
        paths.ensure_directory_structure(syrvis_home, version)

        # Extract SPK to temp location
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            if not extract_spk(spk_path, tmp_path):
                return False

            # Find and extract package.tgz
            package_tgz = tmp_path / "package.tgz"
            if package_tgz.exists():
                pkg_dir = tmp_path / "package"
                pkg_dir.mkdir()
                subprocess.run(
                    ["tar", "-xzf", str(package_tgz), "-C", str(pkg_dir)],
                    check=True
                )

                # Copy wheel file if present
                for whl in pkg_dir.glob("*.whl"):
                    shutil.copy(whl, version_dir / "cli" / whl.name)

                # Copy build config if present
                build_config = pkg_dir / "build" / "config.yaml"
                if build_config.exists():
                    shutil.copy(build_config, version_dir / "build" / "config.yaml")

            # Create venv and install
            venv_path = version_dir / "cli" / "venv"
            if not venv_path.exists():
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_path)],
                    check=True
                )

            # Install wheel into venv
            pip_path = venv_path / "bin" / "pip"
            for whl in (version_dir / "cli").glob("*.whl"):
                subprocess.run(
                    [str(pip_path), "install", "--quiet", str(whl)],
                    check=True
                )

            # Save SPK for future reference
            spk_dest = version_dir / f"syrviscore-{version}.spk"
            shutil.copy(spk_path, spk_dest)

        # Add version to manifest
        paths.add_version_to_manifest(version, "available")

        return True

    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def activate_version(version: str) -> bool:
    """Activate a version (update symlink and manifest)."""
    try:
        version_dir = paths.get_version_dir(version)
        if not version_dir.exists():
            click.echo(f"      Error: Version {version} not installed", err=True)
            return False

        # Update symlink
        paths.update_current_symlink(version)

        # Update manifest
        paths.set_active_version(version)

        return True

    except Exception as e:
        click.echo(f"      Error: {e}", err=True)
        return False


def stop_services() -> bool:
    """Stop running services."""
    try:
        from .docker_manager import DockerManager
        manager = DockerManager()
        manager.stop_core_services()
        return True
    except Exception:
        return False


def start_services() -> bool:
    """Start services."""
    try:
        from .docker_manager import DockerManager
        manager = DockerManager()
        manager.start_core_services()
        return True
    except Exception:
        return False


# =============================================================================
# CLI Commands
# =============================================================================

@click.group()
def update():
    """Manage SyrvisCore updates and versions."""
    pass


@update.command()
def check():
    """Check for available updates."""
    click.echo()
    click.echo("Checking for updates...")
    click.echo()

    current = __version__
    click.echo(f"  Current version: {current}")

    release = get_latest_release()
    if not release:
        click.echo("  Could not fetch release information from GitHub")
        return

    latest = release.get("tag_name", "").lstrip('v')
    click.echo(f"  Latest version:  {latest}")
    click.echo()

    cmp = compare_versions(current, latest)
    if cmp < 0:
        click.echo(f"  Update available: {current} -> {latest}")
        click.echo()
        click.echo("  Release notes:")
        body = release.get("body", "No release notes")
        for line in body.split('\n')[:10]:
            click.echo(f"    {line}")
        click.echo()
        click.echo(f"  Run 'syrvis update install {latest}' to update")
    elif cmp > 0:
        click.echo("  You are running a newer version than the latest release")
    else:
        click.echo("  You are running the latest version")


@update.command()
@click.argument('version', required=False)
def download(version):
    """Download an update without installing."""
    click.echo()

    if not version:
        click.echo("Fetching latest release...")
        release = get_latest_release()
        if not release:
            click.echo("Could not fetch release information", err=True)
            sys.exit(1)
        version = release.get("tag_name", "").lstrip('v')
    else:
        version = version.lstrip('v')
        tag = f"v{version}"
        release = get_release_by_tag(tag)
        if not release:
            click.echo(f"Release {tag} not found", err=True)
            sys.exit(1)

    click.echo(f"Downloading version {version}...")

    asset = find_spk_asset(release)
    if not asset:
        click.echo("No SPK file found in release", err=True)
        sys.exit(1)

    # Download to versions directory
    try:
        syrvis_home = paths.get_syrvis_home()
    except paths.SyrvisHomeError:
        click.echo("SyrvisCore not installed", err=True)
        sys.exit(1)

    versions_dir = paths.get_versions_dir()
    versions_dir.mkdir(parents=True, exist_ok=True)

    dest = versions_dir / asset["name"]
    click.echo(f"  Downloading {asset['name']}...")

    if download_file(asset["browser_download_url"], dest):
        click.echo(f"  Downloaded: {dest}")
        click.echo()
        click.echo(f"  Run 'syrvis update install {version}' to install")
    else:
        sys.exit(1)


@update.command()
@click.argument('version', required=False)
@click.option('--force', is_flag=True, help='Force reinstall even if version exists')
def install(version, force):
    """Download and install an update."""
    click.echo()

    # Get version info
    if not version:
        click.echo("[1/5] Fetching latest release...")
        release = get_latest_release()
        if not release:
            click.echo("      Could not fetch release information", err=True)
            sys.exit(1)
        version = release.get("tag_name", "").lstrip('v')
    else:
        version = version.lstrip('v')
        tag = f"v{version}"
        click.echo(f"[1/5] Fetching release {tag}...")
        release = get_release_by_tag(tag)
        if not release:
            click.echo(f"      Release {tag} not found", err=True)
            sys.exit(1)

    click.echo(f"      Version: {version}")

    # Check if already installed
    try:
        version_dir = paths.get_version_dir(version)
        if version_dir.exists() and not force:
            click.echo(f"      Version {version} already installed")
            if not click.confirm("      Reinstall?", default=False):
                sys.exit(0)
            shutil.rmtree(version_dir)
    except paths.SyrvisHomeError:
        click.echo("      SyrvisCore not installed", err=True)
        sys.exit(1)

    # Download
    click.echo()
    click.echo("[2/5] Downloading...")

    asset = find_spk_asset(release)
    if not asset:
        click.echo("      No SPK file found in release", err=True)
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".spk", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        if not download_file(asset["browser_download_url"], tmp_path):
            sys.exit(1)

        # Install
        click.echo()
        click.echo("[3/5] Installing...")

        if not install_version(version, tmp_path):
            click.echo("      Installation failed", err=True)
            sys.exit(1)

        click.echo("      Installed successfully")

    finally:
        tmp_path.unlink(missing_ok=True)

    # Stop services
    click.echo()
    click.echo("[4/5] Stopping services...")
    if stop_services():
        click.echo("      Services stopped")
    else:
        click.echo("      Warning: Could not stop services")

    # Activate
    click.echo()
    click.echo("[5/5] Activating version...")

    if not activate_version(version):
        click.echo("      Activation failed", err=True)
        sys.exit(1)

    click.echo(f"      Activated: {version}")

    # Start services
    click.echo()
    click.echo("Starting services...")
    if start_services():
        click.echo("      Services started")
    else:
        click.echo("      Warning: Could not start services")
        click.echo("      Run 'syrvis core start' manually")

    click.echo()
    click.echo(f"Update to {version} complete!")
    click.echo()
    click.echo("Previous version preserved for rollback.")
    click.echo("Run 'syrvis update rollback' if needed.")


@update.command()
def rollback():
    """Rollback to the previous version."""
    click.echo()

    try:
        manifest = paths.get_manifest()
    except (paths.SyrvisHomeError, FileNotFoundError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    current = manifest.get("active_version")
    versions = paths.list_installed_versions()

    if len(versions) < 2:
        click.echo("No previous version available for rollback")
        sys.exit(1)

    # Find previous version
    previous = None
    for v in versions:
        if v != current:
            previous = v
            break

    if not previous:
        click.echo("No previous version found")
        sys.exit(1)

    click.echo(f"Current version: {current}")
    click.echo(f"Rollback to:     {previous}")
    click.echo()

    if not click.confirm("Proceed with rollback?"):
        click.echo("Rollback cancelled")
        sys.exit(0)

    # Stop services
    click.echo()
    click.echo("[1/3] Stopping services...")
    if stop_services():
        click.echo("      Services stopped")

    # Switch version
    click.echo()
    click.echo("[2/3] Switching version...")
    if not activate_version(previous):
        click.echo("      Rollback failed", err=True)
        sys.exit(1)
    click.echo(f"      Activated: {previous}")

    # Start services
    click.echo()
    click.echo("[3/3] Starting services...")
    if start_services():
        click.echo("      Services started")
    else:
        click.echo("      Warning: Could not start services")

    click.echo()
    click.echo(f"Rolled back to version {previous}")


@update.command('list')
def list_versions():
    """List installed versions."""
    click.echo()
    click.echo("Installed versions:")
    click.echo()

    try:
        versions = paths.list_installed_versions()
        active = paths.get_active_version()
    except paths.SyrvisHomeError:
        click.echo("SyrvisCore not installed", err=True)
        sys.exit(1)

    if not versions:
        click.echo("  No versions installed")
        return

    for v in versions:
        marker = " (active)" if v == active else ""
        click.echo(f"  {v}{marker}")

    click.echo()
    click.echo(f"Current: {__version__}")


@update.command()
@click.option('--keep', default=DEFAULT_VERSIONS_TO_KEEP, help='Number of versions to keep')
@click.option('--dry-run', is_flag=True, help='Show what would be removed')
def cleanup(keep, dry_run):
    """Remove old versions to free disk space."""
    click.echo()

    try:
        versions = paths.list_installed_versions()
        active = paths.get_active_version()
    except paths.SyrvisHomeError:
        click.echo("SyrvisCore not installed", err=True)
        sys.exit(1)

    if len(versions) <= keep:
        click.echo(f"Only {len(versions)} version(s) installed, nothing to clean up")
        return

    # Versions to remove (keep newest N, never remove active)
    to_remove = []
    kept = 0
    for v in versions:
        if v == active:
            continue
        if kept < keep - 1:  # -1 because active counts as kept
            kept += 1
            continue
        to_remove.append(v)

    if not to_remove:
        click.echo("No versions to remove")
        return

    click.echo(f"Versions to remove: {', '.join(to_remove)}")
    click.echo(f"Versions to keep:   {keep} (including active)")
    click.echo()

    if dry_run:
        click.echo("Dry run - no changes made")
        return

    if not click.confirm("Proceed with cleanup?"):
        click.echo("Cleanup cancelled")
        return

    for v in to_remove:
        version_dir = paths.get_version_dir(v)
        try:
            shutil.rmtree(version_dir)
            click.echo(f"  Removed: {v}")

            # Update manifest
            manifest = paths.get_manifest()
            if v in manifest.get("versions", {}):
                del manifest["versions"][v]
                paths.save_manifest(manifest)

        except Exception as e:
            click.echo(f"  Failed to remove {v}: {e}", err=True)

    click.echo()
    click.echo("Cleanup complete")
