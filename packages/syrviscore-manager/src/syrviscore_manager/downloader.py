"""
GitHub release downloader for SyrvisCore Manager.

Handles fetching release information and downloading assets from GitHub.
"""

import click
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import requests


# GitHub repository for releases
GITHUB_REPO = "kevinteg/SyrvisCore"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"


def get_latest_release() -> Optional[Dict[str, Any]]:
    """Fetch latest SERVICE release info from GitHub.

    Filters out manager releases (manager-v*) to only return service releases (v*).
    """
    try:
        # List recent releases and find the latest service release
        response = requests.get(
            GITHUB_API_URL,
            params={"per_page": 20},
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if response.status_code != 200:
            return None

        releases = response.json()
        for release in releases:
            tag = release.get("tag_name", "")
            # Skip manager releases (manager-v*) and prereleases
            if tag.startswith("manager-"):
                continue
            if release.get("prerelease", False):
                continue
            if release.get("draft", False):
                continue
            # This is a service release
            return release

        return None
    except Exception:
        return None


def get_release_by_tag(tag: str) -> Optional[Dict[str, Any]]:
    """Fetch specific release by tag."""
    try:
        # Ensure tag has 'v' prefix
        if not tag.startswith('v'):
            tag = f"v{tag}"

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


def find_wheel_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the Python wheel file in release assets."""
    for asset in release.get("assets", []):
        name = asset["name"]
        # Look for syrviscore wheel (not syrviscore_manager)
        if name.endswith(".whl") and "syrviscore-" in name and "manager" not in name:
            return asset
    return None


def find_config_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the config.yaml file in release assets."""
    for asset in release.get("assets", []):
        if asset["name"] == "config.yaml":
            return asset
    return None


def find_env_template_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the .env.template file in release assets."""
    for asset in release.get("assets", []):
        if asset["name"] == ".env.template" or asset["name"] == "env.template":
            return asset
    return None


def download_file(url: str, dest: Path, show_progress: bool = True) -> bool:
    """
    Download file with optional progress display.

    Args:
        url: URL to download from
        dest: Destination path
        show_progress: Whether to show progress bar

    Returns:
        True if download succeeded, False otherwise
    """
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


def get_version_from_release(release: Dict[str, Any]) -> str:
    """Extract version string from release."""
    tag = release.get("tag_name", "")
    return tag.lstrip('v')
