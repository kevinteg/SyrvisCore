"""
Path management for SyrvisCore Manager.

Handles discovery of SYRVIS_HOME and SPK installation directories.

Directory Structure:
    /var/packages/syrviscore/target/      # SPK install (manager venv)

    /volumeX/docker/syrviscore/           # SYRVIS_HOME
    ├── current -> versions/0.1.0/         # Symlink to active version
    ├── versions/
    │   ├── 0.0.1/cli/venv/bin/syrvis      # Previous version
    │   └── 0.1.0/cli/venv/bin/syrvis      # Active version
    ├── config/                            # Shared config
    ├── data/                              # Persistent data
    └── .syrviscore-manifest.json
"""

import os
from pathlib import Path
from typing import Optional, List


class SyrvisHomeError(Exception):
    """Raised when SYRVIS_HOME cannot be found or is invalid."""
    pass


# Default package name
PACKAGE_NAME = "syrviscore"

# SPK installation directory
SPK_TARGET_DIR = f"/var/packages/{PACKAGE_NAME}/target"


def get_spk_target_dir() -> Path:
    """Get the SPK installation target directory."""
    return Path(SPK_TARGET_DIR)


def get_manager_venv() -> Path:
    """Get path to manager's virtual environment."""
    return get_spk_target_dir() / "venv"


def get_syrvis_home() -> Path:
    """
    Get the SYRVIS_HOME directory with auto-detection fallback.

    Tries multiple strategies:
    1. SYRVIS_HOME environment variable
    2. Default location /volume1/docker/syrviscore
    3. Search other volumes (volume2-volume9)

    Returns:
        Path object for SYRVIS_HOME directory

    Raises:
        SyrvisHomeError: If SYRVIS_HOME cannot be determined
    """
    # Strategy 1: Environment variable
    syrvis_home = os.environ.get("SYRVIS_HOME")
    if syrvis_home:
        syrvis_path = Path(syrvis_home)
        if syrvis_path.exists() and syrvis_path.is_dir():
            return syrvis_path

    # Strategy 2: Default location
    default = Path("/volume1/docker/syrviscore")
    if default.exists() and (default / ".syrviscore-manifest.json").exists():
        return default

    # Strategy 3: Search other volumes
    for vol_num in range(2, 10):
        candidate = Path(f"/volume{vol_num}/docker/syrviscore")
        if candidate.exists() and (candidate / ".syrviscore-manifest.json").exists():
            return candidate

    raise SyrvisHomeError(
        "Cannot find SyrvisCore installation.\n"
        "Run 'syrvisctl install' to install a service version."
    )


def get_syrvis_home_or_create(volume: Optional[str] = None) -> Path:
    """
    Get SYRVIS_HOME, creating it if it doesn't exist.

    Args:
        volume: Specific volume to use (e.g., "/volume1")

    Returns:
        Path to SYRVIS_HOME directory
    """
    try:
        return get_syrvis_home()
    except SyrvisHomeError:
        # Create new installation directory
        if volume:
            base = Path(volume)
        else:
            # Use first available volume
            for vol_num in range(1, 10):
                candidate = Path(f"/volume{vol_num}")
                if candidate.exists():
                    base = candidate
                    break
            else:
                base = Path("/volume1")

        syrvis_home = base / "docker" / PACKAGE_NAME
        syrvis_home.mkdir(parents=True, exist_ok=True)
        return syrvis_home


def get_versions_dir() -> Path:
    """Get path to versions directory."""
    return get_syrvis_home() / "versions"


def get_version_dir(version: str) -> Path:
    """Get path to a specific version directory."""
    return get_versions_dir() / version


def get_current_symlink() -> Path:
    """Get path to 'current' symlink."""
    return get_syrvis_home() / "current"


def get_active_version_dir() -> Optional[Path]:
    """
    Get path to the active version directory.

    Returns:
        Path to active version directory, or None if no version is active
    """
    current = get_current_symlink()
    if current.exists() and current.is_symlink():
        return current.resolve()
    return None


def get_bin_dir() -> Path:
    """Get path to bin directory containing wrapper scripts."""
    return get_syrvis_home() / "bin"


def get_manifest_path() -> Path:
    """Get path to installation manifest file."""
    return get_syrvis_home() / ".syrviscore-manifest.json"


def list_installed_versions() -> List[str]:
    """List all installed versions, sorted by semantic version (newest first)."""
    try:
        versions_dir = get_versions_dir()
    except SyrvisHomeError:
        return []

    if not versions_dir.exists():
        return []

    versions = []
    for item in versions_dir.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            # Verify it has a venv (properly installed)
            venv_path = item / "cli" / "venv"
            if venv_path.exists():
                versions.append(item.name)

    # Sort by semantic version (newest first)
    def version_key(v):
        try:
            parts = v.split('.')
            return tuple(int(p) for p in parts)
        except ValueError:
            return (0, 0, 0)

    return sorted(versions, key=version_key, reverse=True)


def has_service_installed() -> bool:
    """Check if any service version is installed."""
    return len(list_installed_versions()) > 0


def ensure_directory_structure(install_path: Path, version: str) -> None:
    """
    Create the complete directory structure for a new installation.

    Args:
        install_path: Path to SYRVIS_HOME
        version: Version being installed
    """
    # Root directories
    (install_path / "versions").mkdir(parents=True, exist_ok=True)
    (install_path / "config").mkdir(exist_ok=True)
    (install_path / "config" / "traefik").mkdir(exist_ok=True)
    (install_path / "data").mkdir(exist_ok=True)
    (install_path / "data" / "traefik").mkdir(exist_ok=True)
    (install_path / "data" / "traefik" / "config").mkdir(exist_ok=True)
    (install_path / "data" / "portainer").mkdir(exist_ok=True)
    (install_path / "data" / "cloudflared").mkdir(exist_ok=True)
    (install_path / "bin").mkdir(exist_ok=True)

    # Version-specific directories
    version_dir = install_path / "versions" / version
    version_dir.mkdir(exist_ok=True)
    (version_dir / "cli").mkdir(exist_ok=True)
    (version_dir / "build").mkdir(exist_ok=True)


def update_current_symlink(version: str) -> None:
    """
    Update the 'current' symlink to point to a version.

    Args:
        version: Version to point to
    """
    syrvis_home = get_syrvis_home()
    current = syrvis_home / "current"
    target = Path("versions") / version  # Relative path

    # Remove existing symlink if present
    if current.exists() or current.is_symlink():
        current.unlink()

    # Create new symlink
    current.symlink_to(target)


def create_syrvis_wrapper() -> None:
    """Create the syrvis wrapper script in bin/."""
    syrvis_home = get_syrvis_home()
    bin_dir = syrvis_home / "bin"
    bin_dir.mkdir(exist_ok=True)

    wrapper_path = bin_dir / "syrvis"
    wrapper_content = f'''#!/bin/sh
# SyrvisCore CLI Wrapper
# Auto-generated by syrvisctl

INSTALL_DIR="{syrvis_home}"
export SYRVIS_HOME="${{INSTALL_DIR}}"

CURRENT_VERSION="${{INSTALL_DIR}}/current"
if [ -L "$CURRENT_VERSION" ]; then
    exec "${{CURRENT_VERSION}}/cli/venv/bin/syrvis" "$@"
else
    echo "Error: No service version installed."
    echo "Run 'syrvisctl install' to install a service version."
    exit 1
fi
'''
    wrapper_path.write_text(wrapper_content)
    wrapper_path.chmod(0o755)


# =============================================================================
# Testing Helpers
# =============================================================================

def set_syrvis_home(path: str) -> None:
    """Set SYRVIS_HOME environment variable (for testing)."""
    os.environ["SYRVIS_HOME"] = path


def unset_syrvis_home() -> None:
    """Unset SYRVIS_HOME environment variable (for testing)."""
    if "SYRVIS_HOME" in os.environ:
        del os.environ["SYRVIS_HOME"]
