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


async def _image_updates(refresh: bool) -> dict:
    """The container-image update report (from the shared on-disk cache).

    The library check is synchronous (`requests`), so a refresh runs it in a
    threadpool to avoid blocking the event loop; a non-refresh read returns the
    cached report immediately (the CLI/MCP also populate that cache).
    """
    from fastapi.concurrency import run_in_threadpool

    try:
        from syrviscore import image_updates as iu

        if refresh:
            return await run_in_threadpool(iu.check_updates, refresh=True)
        cached = await run_in_threadpool(iu.read_cached)
        if cached is not None:
            return cached
        # No cache yet — populate it once (bounded by the module's timeouts).
        return await run_in_threadpool(iu.check_updates)
    except Exception as exc:  # noqa: BLE001 - degrade, never 500 the endpoint
        return {"count": 0, "update_count": 0, "images": [], "error": str(exc)}


@router.get("/updates")
async def updates(refresh: bool = False) -> dict:
    """Report the active vs latest SyrvisCore service version + image updates.

    ``version`` = SyrvisCore platform release (GitHub, cached ~1h).
    ``images`` = container-image updates for every pinned image (cached ~6h).
    """
    now = time.monotonic()
    if not refresh and _cache["data"] is not None and now < _cache["expires"]:
        version = _cache["data"]
    else:
        current = _current_version()
        version = {
            "current": current,
            "latest": None,
            "update_available": False,
            "dashboard_version": _dashboard_version,
        }
        try:
            latest = await _latest_service_release()
            version["latest"] = latest
            version["update_available"] = bool(
                current and latest and _ver_key(latest) > _ver_key(current)
            )
        except httpx.HTTPError as exc:
            version["error"] = "could not reach GitHub: {}".format(exc)
        _cache["data"] = version
        _cache["expires"] = time.monotonic() + _TTL_S

    images = await _image_updates(refresh)

    # Backward-compatible: the top level still carries the platform-version
    # fields (the header badge reads them); `version` + `images` are the
    # structured view the Updates card consumes.
    result = dict(version)
    result["version"] = version
    result["images"] = images
    return result
