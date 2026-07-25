"""Container-image update checking (report-only, never auto-pull).

Given the pinned image references SyrvisCore runs — core-tier images (from the
resolved compose config) and Layer 2 service images (from installed manifests)
— this queries each image's registry (the OCI Distribution ``/v2/<repo>/tags/list``
API, with the standard bearer-token challenge, so Docker Hub, GHCR, quay, etc.
all work uniformly) and reports whether a newer *compatible* tag exists.

Philosophy: **detect and report, never apply.** A NAS is critical and
declaratively managed, so an update is a deliberate act — bump the pin in the
declaration and re-deploy (``syrvis service set-image`` for L2; the release
channel for core). This module only tells you what *could* be updated. It is
the engine behind ``syrvis updates``, the dashboard Updates card, and the
``image_updates`` MCP tool. Results are file-cached (shared by all three) so
registries are never hammered.

Kept import-light and Python 3.8-clean (runs on the DSM CLI and, in-process,
in the dashboard container).
"""

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Registry host aliases → the OCI API host to actually query.
_DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry.hub.docker.com"}
_DOCKER_HUB_API = "registry-1.docker.io"

# A tag we can order: dotted integers, optional leading 'v', optional -suffix.
# Groups: (v?)(numeric.dotted)(-suffix?)
_TAG_RE = re.compile(r"^(v?)(\d+(?:\.\d+)*)(?:-(.+))?$")

# Bound the number of tag-list pages we page through per image (tags/list is
# lexicographic, not chronological, so we must collect to compute the max
# ourselves). Enough for any real image; a truncation is flagged in the result.
_MAX_PAGES = 12
_PAGE_SIZE = 100
_HTTP_TIMEOUT = 10

DEFAULT_TTL_S = 6 * 3600  # image tags move slowly; a 6h cache is plenty


@dataclass
class ImageRef:
    """A parsed image reference split into its addressable parts."""

    registry: str  # canonical registry host, e.g. "docker.io" or "ghcr.io"
    repository: str  # e.g. "library/traefik", "kevinteg/syrviscore-dashboard"
    tag: str  # e.g. "v3.6.5" ("" when pinned only by digest)
    digest: str  # e.g. "sha256:..." ("" when pinned by tag)

    @property
    def api_host(self) -> str:
        return _DOCKER_HUB_API if self.registry in _DOCKER_HUB_HOSTS else self.registry


def parse_image_ref(ref: str) -> ImageRef:
    """Parse ``ref`` into (registry, repository, tag, digest), Docker-Hub-normalized.

    ``traefik:v3.6.5`` → docker.io / library/traefik ; ``org/name:tag`` →
    docker.io / org/name ; ``ghcr.io/o/n:tag`` → ghcr.io / o/n ; digests kept.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("empty image reference")
    rest = ref.strip()

    digest = ""
    if "@" in rest:
        rest, digest = rest.rsplit("@", 1)

    tag = ""
    # A ':' is a tag only in the LAST path segment (a ':' before a '/' is a
    # registry port, not a tag).
    last_slash = rest.rfind("/")
    last_colon = rest.rfind(":")
    if last_colon > last_slash:
        rest, tag = rest[:last_colon], rest[last_colon + 1 :]

    # Registry vs repository: the first segment is a registry iff it has a '.'
    # or ':' (a host/port) or is literally "localhost". Otherwise it is a Docker
    # Hub repo (bare name → library/<name>; org/name stays).
    first, _, remainder = rest.partition("/")
    if remainder and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, remainder
    else:
        registry = "docker.io"
        repository = rest if "/" in rest else "library/{}".format(rest)

    return ImageRef(registry=registry, repository=repository, tag=tag, digest=digest)


def _parse_tag(tag: str) -> Optional[Tuple[bool, Tuple[int, ...], str]]:
    """(has_v_prefix, numeric_tuple, suffix) for an orderable tag, else None.

    ``latest``, ``sha-abcdef``, date hashes, etc. → None (not comparable).
    """
    m = _TAG_RE.match(tag)
    if not m:
        return None
    v, nums, suffix = m.group(1), m.group(2), m.group(3) or ""
    return (bool(v), tuple(int(p) for p in nums.split(".")), suffix)


def find_newer_tags(current: str, candidates: List[str]) -> List[str]:
    """Tags in ``candidates`` that are a strictly-newer version of the SAME flavor.

    Same flavor = same v-prefix convention, same ``-suffix``, AND the same number
    of numeric components. The component-count match is what stops a semver pin
    from cross-matching an unrelated tag scheme: a ``2.5`` pin never "updates" to
    an 8-digit CalVer ``20240101`` (1 component vs 2), and a rolling ``16`` pin
    never jumps to ``16.2`` (1 vs 2) — different granularities float independently.
    An ``-alpine`` pin only advances within ``-alpine``, a plain pin never jumps
    to ``-rc1``. Sorted oldest→newest.
    """
    cur = _parse_tag(current)
    if cur is None:
        return []  # can't reason about a non-version current pin (e.g. a digest)
    cur_v, cur_nums, cur_suffix = cur
    newer = []
    for cand in candidates:
        if cand == current:
            continue
        p = _parse_tag(cand)
        if p is None:
            continue
        v, nums, suffix = p
        # Same flavor (v-prefix, suffix, AND component count) and strictly greater.
        if v != cur_v or suffix != cur_suffix or len(nums) != len(cur_nums):
            continue
        if nums > cur_nums:
            newer.append((nums, cand))
    newer.sort()
    return [c for _, c in newer]


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------


class _RegistryError(Exception):
    pass


def _bearer_token(session, www_authenticate: str) -> Optional[str]:
    """Follow a ``WWW-Authenticate: Bearer realm=...,service=...,scope=...``
    challenge and return an access token (anonymous pull scope)."""
    if not www_authenticate.lower().startswith("bearer "):
        return None
    params = {}
    for part in www_authenticate[len("bearer ") :].split(","):
        if "=" in part:
            k, _, val = part.strip().partition("=")
            params[k.strip()] = val.strip().strip('"')
    realm = params.get("realm")
    if not realm:
        return None
    query = {k: params[k] for k in ("service", "scope") if params.get(k)}
    resp = session.get(realm, params=query, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("token") or data.get("access_token")


def list_tags(ref: ImageRef, session=None) -> Tuple[List[str], bool]:
    """All tags for ``ref.repository`` via the OCI Distribution tags/list API.

    Returns ``(tags, truncated)`` where ``truncated`` is True if the page budget
    was exhausted before the registry ran out of pages. Handles the 401
    bearer-token challenge (anonymous pull) and follows ``Link`` pagination.
    Raises ``_RegistryError`` on a hard failure; callers turn that into a
    per-image ``error`` string.
    """
    import requests

    session = session or requests.Session()
    url: Optional[str] = "https://{}/v2/{}/tags/list".format(ref.api_host, ref.repository)
    params: Optional[dict] = {"n": _PAGE_SIZE}
    token: Optional[str] = None
    tags: List[str] = []
    pages_fetched = 0

    while url is not None and pages_fetched < _MAX_PAGES:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer {}".format(token)
        try:
            resp = session.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - network/DNS/TLS → uniform error
            raise _RegistryError("request failed: {}".format(e))

        if resp.status_code == 401 and token is None:
            # The auth challenge is NOT a page: retry the same URL with a token
            # without consuming the page budget.
            token = _bearer_token(session, resp.headers.get("WWW-Authenticate", ""))
            if not token:
                raise _RegistryError("registry requires auth we can't satisfy")
            continue
        if resp.status_code == 404:
            raise _RegistryError("repository not found on {}".format(ref.registry))
        if resp.status_code != 200:
            raise _RegistryError("registry returned HTTP {}".format(resp.status_code))

        body = resp.json()
        tags.extend(body.get("tags") or [])
        pages_fetched += 1

        # RFC5988 Link: <...>; rel="next" — the OCI pagination signal. The target
        # may be relative (Docker Hub/GHCR) or absolute (some registries).
        nxt = _next_link(resp.headers.get("Link", ""))
        if not nxt:
            url = None
        elif nxt.startswith(("http://", "https://")):
            url, params = nxt, None
        else:
            url, params = "https://{}{}".format(ref.api_host, nxt), None

    truncated = url is not None  # loop stopped on the page budget, not on end-of-pages
    return tags, truncated


def _next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">", start + 1)
            if start != -1 and end != -1:
                return part[start + 1 : end]
    return None


# ---------------------------------------------------------------------------
# Per-image check + collection + cache
# ---------------------------------------------------------------------------


def check_image(image: str, session=None) -> Dict[str, Any]:
    """Report on a single pinned image reference."""
    result: Dict[str, Any] = {
        "image": image,
        "current": None,
        "latest": None,
        "update_available": False,
        "newer": [],
        "error": None,
    }
    try:
        ref = parse_image_ref(image)
    except ValueError as e:
        result["error"] = str(e)
        return result
    result["registry"] = ref.registry
    result["repository"] = ref.repository
    result["current"] = ref.tag or (ref.digest[:19] + "…" if ref.digest else None)

    if not ref.tag:
        result["error"] = "pinned by digest — no tag to compare"
        return result
    try:
        tags, truncated = list_tags(ref, session=session)
    except _RegistryError as e:
        result["error"] = str(e)
        return result

    if truncated:
        result["truncated"] = True
    newer = find_newer_tags(ref.tag, tags)
    result["newer"] = newer[-5:]  # a few newest, oldest→newest
    result["latest"] = newer[-1] if newer else ref.tag
    result["update_available"] = bool(newer)
    return result


def collect_pinned_images(home: Optional[Path] = None) -> List[Dict[str, str]]:
    """Every image SyrvisCore currently runs: core (enabled) + installed L2.

    Returns ``[{"kind": "core"|"service", "name": ..., "image": ...}]``.
    De-duplicated by image reference (the same image checked once).
    """
    from . import stack as stack_mod
    from .compose import ComposeGenerator

    out: List[Dict[str, str]] = []
    seen = set()

    def add(kind: str, name: str, image: str) -> None:
        if image and image not in seen:
            seen.add(image)
            out.append({"kind": kind, "name": name, "image": image})

    # Core-tier images (resolved config), limited to what the stack enables.
    try:
        cfg = ComposeGenerator().load_config()
        stack = stack_mod.load_stack()
        for name, entry in cfg.get("docker_images", {}).items():
            # traefik/portainer always run; optional ones only when enabled.
            enabled = name in stack_mod.PRIMORDIAL or stack.is_enabled(name)
            if enabled and entry.get("full_image"):
                add("core", name, entry["full_image"])
    except Exception:  # noqa: BLE001 - no install context: skip core
        pass

    # Installed Layer 2 service images.
    try:
        from .service_manager import ServiceManager

        mgr = ServiceManager()
        services_dir = mgr.services_dir
        if services_dir.exists():
            from .service_schema import load_service_definition

            for svc_dir in sorted(services_dir.iterdir()):
                manifest = svc_dir / "syrvis-service.yaml"
                if not manifest.is_file():
                    continue
                try:
                    svc = load_service_definition(manifest)
                    add("service", svc.name, svc.image)
                except Exception:  # noqa: BLE001 - a broken manifest: skip it
                    continue
    except Exception:  # noqa: BLE001 - no install context: skip L2
        pass

    return out


def _cache_path(home: Path) -> Path:
    return Path(home) / "data" / ".image-updates-cache.json"


def invalidate_cache(home: Optional[Path] = None) -> None:
    """Drop the cached updates report so the next check re-derives from the
    current pins.

    Called after a deploy changes the deployed image set: the report caches
    "current" (from the declared pin) frozen at check time, so a just-applied
    update would keep showing as "available" until the TTL expires. Dropping the
    cache makes the next read (CLI/dashboard/MCP) reflect reality immediately.
    """
    from . import paths

    try:
        resolved = Path(home) if home is not None else paths.get_syrvis_home()
    except Exception:  # noqa: BLE001
        return
    try:
        _cache_path(resolved).unlink()
    except OSError:
        pass


def check_updates(
    home: Optional[Path] = None,
    refresh: bool = False,
    ttl_s: int = DEFAULT_TTL_S,
    now: float = None,
) -> Dict[str, Any]:
    """Check every pinned image and return a cached report.

    Report: ``{"checked_at", "count", "update_count", "images": [...], "error"?}``.
    The result is cached under ``data/`` so the CLI, the dashboard, and the MCP
    all share one check and never hammer registries. ``refresh=True`` forces it.
    """
    from . import paths

    resolved_home = Path(home) if home is not None else None
    if resolved_home is None:
        try:
            resolved_home = paths.get_syrvis_home()
        except Exception:  # noqa: BLE001
            resolved_home = None

    stamp = now if now is not None else time.time()

    cache_file = _cache_path(resolved_home) if resolved_home else None
    if not refresh and cache_file and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text())
            if stamp - cached.get("checked_at", 0) < ttl_s:
                cached["cached"] = True
                return cached
        except (OSError, ValueError):
            pass

    images = collect_pinned_images(resolved_home)
    import requests

    session = requests.Session()
    checked = [check_image(entry["image"], session=session) for entry in images]
    # Merge the kind/name metadata back onto each result.
    by_image = {c["image"]: c for c in checked}
    results = []
    for entry in images:
        merged = dict(by_image.get(entry["image"], {}))
        merged["kind"] = entry["kind"]
        merged["name"] = entry["name"]
        results.append(merged)

    report = {
        "checked_at": stamp,
        "count": len(results),
        "update_count": sum(1 for r in results if r.get("update_available")),
        "images": results,
        "cached": False,
    }
    if cache_file:
        # Atomic write (temp + rename): the CLI and the dashboard may both
        # refresh the shared cache, and a torn write would corrupt it.
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(cache_file.parent), prefix=".image-updates.")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps(report, indent=2))
                os.replace(tmp, str(cache_file))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass
    return report


def read_cached(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the last cached report without hitting the network (dashboard fast
    path), or None if there is none."""
    from . import paths

    try:
        resolved = Path(home) if home is not None else paths.get_syrvis_home()
        cache_file = _cache_path(resolved)
        if cache_file.is_file():
            data = json.loads(cache_file.read_text())
            data["cached"] = True
            return data
    except Exception:  # noqa: BLE001 - unresolved home / unreadable cache: no data
        pass
    return None
