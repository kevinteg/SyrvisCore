"""Update check — is a newer SyrvisCore service release available on GitHub?

Compares the active service version against the latest GitHub release (same filter
the manager's downloader uses: ``v*`` tags, excluding ``manager-*``, prereleases,
and drafts). Cached ~1h so we never hammer the GitHub API.
"""

import time

import httpx
from fastapi import APIRouter

from ..__version__ import __version__ as _dashboard_version

router = APIRouter(prefix="/api", tags=["updates"])

_RELEASES_URL = "https://api.github.com/repos/kevinteg/SyrvisCore/releases"
_TTL_S = 3600
_cache = {"data": None, "expires": 0.0}


def _ver_key(version: str):
    try:
        return tuple(int(p) for p in version.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _current_version():
    try:
        from syrviscore.config_reader import read_config

        return read_config().active_version
    except Exception:  # noqa: BLE001
        return None


async def _latest_service_release() -> str:
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
        resp = await http.get(_RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        versions = []
        for rel in resp.json():
            tag = rel.get("tag_name", "")
            if (
                tag.startswith("v")
                and "manager" not in tag
                and not rel.get("prerelease")
                and not rel.get("draft")
            ):
                versions.append(tag[1:])
        versions.sort(key=_ver_key)
        return versions[-1] if versions else None


@router.get("/updates")
async def updates(refresh: bool = False) -> dict:
    """Report the active vs latest SyrvisCore service version (cached)."""
    now = time.monotonic()
    if not refresh and _cache["data"] is not None and now < _cache["expires"]:
        return _cache["data"]

    current = _current_version()
    result = {
        "current": current,
        "latest": None,
        "update_available": False,
        "dashboard_version": _dashboard_version,
    }
    try:
        latest = await _latest_service_release()
        result["latest"] = latest
        result["update_available"] = bool(
            current and latest and _ver_key(latest) > _ver_key(current)
        )
    except httpx.HTTPError as exc:
        result["error"] = "could not reach GitHub: {}".format(exc)

    _cache["data"] = result
    _cache["expires"] = time.monotonic() + _TTL_S
    return result
