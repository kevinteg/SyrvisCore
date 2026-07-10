"""
GitHub release downloader for SyrvisCore Manager.

Handles fetching release information, downloading assets, and verifying
their integrity against a SHA256SUMS release asset.

v2 rules:
- No printing: progress is reported through an optional callback.
- No silent failures: every error raises a typed exception with the HTTP
  status or root cause preserved.
- Downloads are checksum-verified by default (see verify_asset_checksum);
  installs of releases without checksums require an explicit opt-out.
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from .errors import IntegrityError, InvalidVersionError, NetworkError, ReleaseNotFoundError

# GitHub repository for releases
GITHUB_REPO = "kevinteg/SyrvisCore"
GITHUB_API_URL = "https://api.github.com/repos/{}/releases".format(GITHUB_REPO)

# Recognized names for the checksums asset attached to a release
CHECKSUM_ASSET_NAMES = ("SHA256SUMS", "SHA256SUMS.txt", "checksums.txt")

ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    return headers


def _get(url: str, **kwargs) -> requests.Response:
    try:
        return requests.get(url, headers=_headers(), timeout=30, **kwargs)
    except requests.RequestException as e:
        raise NetworkError("GitHub request failed: {}".format(e))


def get_latest_release() -> Dict[str, Any]:
    """Fetch latest SERVICE release info from GitHub.

    Filters out manager releases (manager-v*), prereleases, and drafts.

    Raises:
        NetworkError: On HTTP/network failure (includes status and rate-limit hints).
        ReleaseNotFoundError: If no service release exists.
    """
    response = _get(GITHUB_API_URL, params={"per_page": 50})
    if response.status_code != 200:
        raise NetworkError(_http_error_message(response))

    for release in response.json():
        tag = release.get("tag_name", "")
        if tag.startswith("manager-"):
            continue
        if release.get("prerelease", False) or release.get("draft", False):
            continue
        return release

    raise ReleaseNotFoundError("No service release found in {}".format(GITHUB_REPO))


def get_release_by_tag(tag: str) -> Dict[str, Any]:
    """Fetch a specific release by tag.

    Raises:
        ReleaseNotFoundError: If the tag doesn't exist.
        NetworkError: On other HTTP/network failures.
    """
    if not tag.startswith("v"):
        tag = "v{}".format(tag)

    response = _get("{}/tags/{}".format(GITHUB_API_URL, tag))
    if response.status_code == 200:
        return response.json()
    if response.status_code == 404:
        raise ReleaseNotFoundError("Release {} not found in {}".format(tag, GITHUB_REPO))
    raise NetworkError(_http_error_message(response))


def _http_error_message(response: requests.Response) -> str:
    msg = "GitHub API returned HTTP {} for {}".format(response.status_code, response.url)
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        msg += (
            " (rate limit exceeded; set GITHUB_TOKEN to raise the limit, "
            "resets at epoch {})".format(response.headers.get("X-RateLimit-Reset", "?"))
        )
    return msg


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a strict N.N.N version string into a comparable tuple.

    Raises:
        InvalidVersionError: For anything that isn't plain MAJOR.MINOR.PATCH.
    """
    v = version_str[1:] if version_str.startswith("v") else version_str
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise InvalidVersionError(
            "Invalid version {!r}: expected MAJOR.MINOR.PATCH".format(version_str)
        )
    return tuple(int(p) for p in parts)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings. Returns -1, 0, or 1."""
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    if t1 < t2:
        return -1
    if t1 > t2:
        return 1
    return 0


def find_wheel_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the service wheel file in release assets."""
    for asset in release.get("assets", []):
        name = asset["name"]
        if name.endswith(".whl") and "syrviscore-" in name and "manager" not in name:
            return asset
    return None


def find_config_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the config.yaml file in release assets."""
    for asset in release.get("assets", []):
        if asset["name"] == "config.yaml":
            return asset
    return None


def find_checksums_asset(release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the SHA256SUMS asset in a release."""
    for asset in release.get("assets", []):
        if asset["name"] in CHECKSUM_ASSET_NAMES:
            return asset
    return None


def download_file(url: str, dest: Path, progress: Optional[ProgressCallback] = None) -> Path:
    """
    Download a file.

    Args:
        url: URL to download from
        dest: Destination path
        progress: Optional callback receiving (downloaded_bytes, total_bytes)

    Raises:
        NetworkError: On any download failure.
    """
    try:
        response = requests.get(url, headers=_headers(), stream=True, timeout=60)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(str(dest), "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total_size)

        return dest
    except requests.RequestException as e:
        raise NetworkError("Download failed for {}: {}".format(url, e))


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256sums(text: str) -> Dict[str, str]:
    """Parse SHA256SUMS content (``<hex>  <filename>`` per line)."""
    sums = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            filename = parts[-1].lstrip("*")
            sums[filename] = parts[0].lower()
    return sums


def verify_asset_checksum(path: Path, sums: Dict[str, str]) -> None:
    """Verify a downloaded asset against a parsed SHA256SUMS mapping.

    Raises:
        IntegrityError: If the file is not listed or the digest differs.
    """
    expected = sums.get(path.name)
    if not expected:
        raise IntegrityError(
            "{} is not listed in the release SHA256SUMS — refusing to install it".format(path.name)
        )
    actual = sha256_file(path)
    if actual != expected:
        raise IntegrityError(
            "Checksum mismatch for {}: expected {}, got {}".format(path.name, expected, actual)
        )


def get_version_from_release(release: Dict[str, Any]) -> str:
    """Extract version string from release."""
    tag = release.get("tag_name", "")
    return tag[1:] if tag.startswith("v") else tag
