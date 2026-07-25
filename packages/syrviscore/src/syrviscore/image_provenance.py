"""Image provenance + freshness readout (``syrvis images``).

Per pinned image (core tier + installed L2) report **provenance** (publisher
class, expected vs actual base) and **freshness** (local age, a newer tag,
last-pushed), validated against a committed **trust registry**
(``image_trust.yaml``). This complements ``image_updates.py`` (update detection)
and the Renovate+Trivy plan (home-tech design/19): reputability becomes a
diffable git assertion, not an unanswerable live lookup.

Two tiers:

- **CHEAP** — zero network: ref parse, digest-pinned?, the committed
  publisher_class/expected_base, and (best-effort, if the docker SDK is present)
  the LOCAL image's created date + running digest + base-from-config-label. Safe
  on every interactive call; powers the ``syrvis status`` trust glyph.
- **HEAVY** — registry reads, cached (6h): update-available (reuses
  ``image_updates``), the base from the image's manifest annotations, and the
  Hub last-pushed date. Only on ``syrvis images --refresh``.

Every enricher is optional: the docker SDK, skopeo, trivy — absent ⇒ the field
degrades to ``None``, never a crash (DSM ships none of them). Pure ``requests`` +
``dataclasses``; Python 3.8-clean.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import image_updates
from .image_updates import ImageRef, parse_image_ref

# Publisher classes we consider "trusted" for the status glyph.
TRUSTED_CLASSES = frozenset({"official", "verified", "sponsored-oss", "trusted-org"})
ALL_CLASSES = TRUSTED_CLASSES | frozenset({"community", "unknown"})

DEFAULT_TTL_S = 6 * 3600
_HTTP_TIMEOUT = 10

# OCI base-image annotation keys (manifest/index level — NOT config labels).
_BASE_NAME_KEYS = ("org.opencontainers.image.base.name",)
_BASE_LABEL_KEYS = ("org.opencontainers.image.base.name",)


def _bundled_registry_path() -> Path:
    return Path(__file__).parent / "image_trust.yaml"


def load_trust_registry(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The committed image-trust registry, merged with a site override.

    Bundled ``image_trust.yaml`` (in the wheel) provides the vetted defaults; a
    site file at ``$SYRVIS_HOME/trust/image_trust.yaml`` overrides/extends it
    (same two-source pattern as the service catalog). Keyed by
    ``<registry>/<repository>`` (canonical, as ``parse_image_ref`` produces).
    Never raises — a missing/broken file yields ``{}`` for that source.
    """
    import yaml

    registry: Dict[str, Dict[str, Any]] = {}

    def merge(path: Path) -> None:
        try:
            if path.is_file():
                data = yaml.safe_load(path.read_text()) or {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            registry[str(k)] = v
        except Exception:  # noqa: BLE001 - a bad trust file must never break status
            pass

    merge(_bundled_registry_path())
    # Site override.
    try:
        from . import paths

        resolved = Path(home) if home is not None else paths.get_syrvis_home()
        merge(Path(resolved) / "trust" / "image_trust.yaml")
    except Exception:  # noqa: BLE001 - no home context: bundled only
        pass
    return registry


def _repo_key(ref: ImageRef) -> str:
    return "{}/{}".format(ref.registry, ref.repository)


def _derive_publisher(ref: ImageRef) -> str:
    """Publisher class when the trust registry has no entry (a conservative guess).

    Docker Official Images (``docker.io/library/*``) → official; everything else
    (including any ``ghcr.io`` org, which carries no reputability signal at all)
    → community. Never guesses "trusted" — that must be asserted in the registry.
    """
    if ref.registry in image_updates._DOCKER_HUB_HOSTS and ref.repository.startswith("library/"):
        return "official"
    return "community"


@dataclass
class ImageProvenance:
    # identity (cheap)
    image: str
    kind: str = ""  # "core" | "service"
    name: str = ""
    registry: str = ""
    repository: str = ""
    tag: str = ""
    digest: str = ""
    digest_pinned: bool = False
    # trust registry (cheap)
    publisher_class: str = "unknown"
    trust_source: str = "derived"  # "registry" | "derived"
    expected_base: Optional[str] = None
    eol_product: Optional[str] = None
    source: Optional[str] = None
    # local inspect (cheap, best-effort)
    local_created: Optional[str] = None
    created_trusted: Optional[bool] = None
    resolved_digest: Optional[str] = None
    base_from_label: Optional[str] = None
    # heavy (None until a refresh)
    update_available: Optional[bool] = None
    latest: Optional[str] = None
    base_from_manifest: Optional[str] = None
    base_drift: Optional[bool] = None
    last_pushed: Optional[str] = None
    # verdict (cheap)
    trust: str = "warn"  # "ok" | "warn"
    notes: List[str] = field(default_factory=list)


def _local_inspect(image: str) -> Dict[str, Any]:
    """Best-effort LOCAL image facts via the docker SDK (zero network).

    Returns ``{}`` when docker is unavailable or the image isn't pulled — the
    cheap tier degrades, it never fails.
    """
    try:
        import docker  # optional dependency
    except Exception:  # noqa: BLE001
        return {}
    try:
        client = docker.from_env()
        attrs = client.images.get(image).attrs or {}
    except Exception:  # noqa: BLE001 - daemon unreachable / image absent
        return {}
    created = attrs.get("Created")
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    repo_digests = attrs.get("RepoDigests") or []
    resolved = ""
    if repo_digests and "@" in repo_digests[0]:
        resolved = repo_digests[0].rsplit("@", 1)[1]
    base = None
    for k in _BASE_LABEL_KEYS:
        if labels.get(k):
            base = labels[k]
            break
    return {"created": created, "resolved_digest": resolved, "base_from_label": base}


def _created_is_trusted(created: Optional[str]) -> Optional[bool]:
    """False when the created date is epoch/rewound (distroless/ko/buildpacks zero
    it), True when it looks real, None when absent."""
    if not created:
        return None
    return not created.startswith("1970-01-01")


def cheap_provenance(
    image: str,
    kind: str = "",
    name: str = "",
    trust: Optional[Dict[str, Dict[str, Any]]] = None,
    inspect: bool = True,
) -> ImageProvenance:
    """The zero-network provenance record for one image (interactive-safe)."""
    registry = trust or {}
    try:
        ref = parse_image_ref(image)
    except ValueError:
        p = ImageProvenance(image=image, kind=kind, name=name)
        p.notes.append("unparseable image reference")
        return p

    p = ImageProvenance(
        image=image,
        kind=kind,
        name=name,
        registry=ref.registry,
        repository=ref.repository,
        tag=ref.tag,
        digest=ref.digest,
        digest_pinned=bool(ref.digest),
    )

    entry = registry.get(_repo_key(ref))
    if entry:
        p.trust_source = "registry"
        p.publisher_class = str(entry.get("publisher_class") or "unknown")
        p.expected_base = entry.get("expected_base")
        p.eol_product = entry.get("eol_product")
        p.source = entry.get("source")
    else:
        p.trust_source = "derived"
        p.publisher_class = _derive_publisher(ref)
        p.notes.append("not in trust registry (publisher derived)")

    if inspect:
        facts = _local_inspect(image)
        p.local_created = facts.get("created")
        p.created_trusted = _created_is_trusted(facts.get("created"))
        p.resolved_digest = facts.get("resolved_digest") or None
        p.base_from_label = facts.get("base_from_label")

    # Cheap base-drift check (only when both the expectation and a local label exist).
    if p.expected_base and p.base_from_label:
        p.base_drift = p.expected_base.split(":")[0] not in p.base_from_label
        if p.base_drift:
            p.notes.append(
                "base drift: expected {!r}, image labels {!r}".format(
                    p.expected_base, p.base_from_label
                )
            )

    _score(p)
    return p


def _score(p: ImageProvenance) -> None:
    """Set the cheap trust verdict + a note when it isn't clean."""
    trusted_pub = p.publisher_class in TRUSTED_CLASSES
    ok = trusted_pub and p.digest_pinned and not p.base_drift
    if not p.digest_pinned:
        p.notes.append("not digest-pinned (tag pins are mutable)")
    if not trusted_pub:
        p.notes.append("publisher class {!r} — vet before trusting".format(p.publisher_class))
    p.trust = "ok" if ok else "warn"


# ---------------------------------------------------------------------------
# Heavy enrichment (network; cached)
# ---------------------------------------------------------------------------
def _manifest_base(ref: ImageRef, session) -> Optional[str]:
    """Read ``org.opencontainers.image.base.name`` from the image's manifest/index
    annotations (Docker Official Images set it there, not as a config label).
    Best-effort → None on any failure."""
    accept = ",".join(
        [
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]
    )
    ref_id = ref.digest or ref.tag
    if not ref_id:
        return None
    url = "https://{}/v2/{}/manifests/{}".format(ref.api_host, ref.repository, ref_id)
    try:
        resp = session.get(url, headers={"Accept": accept}, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 401:
            token = image_updates._bearer_token(session, resp.headers.get("WWW-Authenticate", ""))
            if not token:
                return None
            resp = session.get(
                url,
                headers={"Accept": accept, "Authorization": "Bearer {}".format(token)},
                timeout=_HTTP_TIMEOUT,
            )
        if resp.status_code != 200:
            return None
        annotations = (resp.json() or {}).get("annotations") or {}
        for k in _BASE_NAME_KEYS:
            if annotations.get(k):
                return annotations[k]
    except Exception:  # noqa: BLE001
        return None
    return None


def _hub_last_pushed(ref: ImageRef, session) -> Optional[str]:
    """Docker Hub ``tag_last_pushed`` for the pinned tag (the real 'patched' date).
    Hub only (public, no auth); GHCR/others → None."""
    if ref.registry not in image_updates._DOCKER_HUB_HOSTS or not ref.tag:
        return None
    url = "https://hub.docker.com/v2/repositories/{}/tags/{}".format(ref.repository, ref.tag)
    try:
        resp = session.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        return data.get("tag_last_pushed") or data.get("last_updated")
    except Exception:  # noqa: BLE001
        return None


def _enrich_heavy(p: ImageProvenance, updates_by_image: Dict[str, Dict[str, Any]], session) -> None:
    """Add the network-derived fields to a cheap record (in place)."""
    upd = updates_by_image.get(p.image)
    if upd is not None:
        p.update_available = bool(upd.get("update_available"))
        p.latest = upd.get("latest")
    try:
        ref = parse_image_ref(p.image)
    except ValueError:
        return
    p.base_from_manifest = _manifest_base(ref, session)
    p.last_pushed = _hub_last_pushed(ref, session)
    # Manifest base can confirm/deny drift when the local label was absent.
    if p.expected_base and p.base_from_manifest and p.base_drift is None:
        p.base_drift = p.expected_base.split(":")[0] not in p.base_from_manifest
        if p.base_drift:
            p.notes.append(
                "base drift: expected {!r}, manifest base {!r}".format(
                    p.expected_base, p.base_from_manifest
                )
            )
            _score(p)


# ---------------------------------------------------------------------------
# Report + cache
# ---------------------------------------------------------------------------
def _cache_path(home: Path) -> Path:
    return Path(home) / "data" / ".image-provenance-cache.json"


def build_report(
    home: Optional[Path] = None,
    refresh: bool = False,
    inspect: bool = True,
    ttl_s: int = DEFAULT_TTL_S,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Provenance for every pinned image. Cheap tier always live; heavy tier from
    cache unless ``refresh``. Never raises.
    """
    from . import paths

    resolved_home: Optional[Path]
    try:
        resolved_home = Path(home) if home is not None else paths.get_syrvis_home()
    except Exception:  # noqa: BLE001
        resolved_home = None

    stamp = now if now is not None else time.time()
    trust = load_trust_registry(resolved_home)
    images = image_updates.collect_pinned_images(resolved_home)

    # Cheap tier — always computed live (local + committed data only).
    records = [
        cheap_provenance(e["image"], e.get("kind", ""), e.get("name", ""), trust, inspect=inspect)
        for e in images
    ]

    # Heavy tier — from cache, or a live refresh.
    cache_file = _cache_path(resolved_home) if resolved_home else None
    heavy: Dict[str, Dict[str, Any]] = {}
    heavy_at: Optional[float] = None
    if refresh:
        try:
            import requests

            session = requests.Session()
            updates = image_updates.check_updates(home=resolved_home, refresh=True, now=stamp)
            upd_by_img = {u["image"]: u for u in updates.get("images", [])}
            for p in records:
                _enrich_heavy(p, upd_by_img, session)
            heavy = {p.image: _heavy_fields(p) for p in records}
            heavy_at = stamp
        except Exception:  # noqa: BLE001 - offline: cheap tier still returns
            pass
    elif cache_file and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text())
            heavy = {h["image"]: h for h in cached.get("images", []) if "image" in h}
            heavy_at = cached.get("heavy_checked_at")
        except (OSError, ValueError):
            pass

    dicts = []
    for p in records:
        d = asdict(p)
        h = heavy.get(p.image)
        if h and not refresh:
            for k in _HEAVY_KEYS:
                d[k] = h.get(k)
            if h.get("base_drift") and not d.get("base_drift"):
                d["base_drift"] = True
        dicts.append(d)

    report = {
        "generated_at": stamp,
        "heavy_checked_at": heavy_at,
        "heavy_fresh": bool(refresh),
        "count": len(dicts),
        "trusted": sum(1 for d in dicts if d.get("trust") == "ok"),
        "attention": sum(1 for d in dicts if d.get("trust") != "ok"),
        "update_count": sum(1 for d in dicts if d.get("update_available")),
        "images": dicts,
    }

    if refresh and cache_file:
        _atomic_write(cache_file, report)
    return report


_HEAVY_KEYS = ("update_available", "latest", "base_from_manifest", "base_drift", "last_pushed")


def _heavy_fields(p: ImageProvenance) -> Dict[str, Any]:
    d = {"image": p.image}
    for k in _HEAVY_KEYS:
        d[k] = getattr(p, k)
    return d


def _atomic_write(cache_file: Path, report: Dict[str, Any]) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cache_file.parent), prefix=".image-provenance.")
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


def status_summary(home: Optional[Path] = None) -> Dict[str, int]:
    """A cheap {trusted, attention, count} for the status line.

    ``inspect=False`` — no docker calls, so ``syrvis status`` stays instant; the
    verdict rests on publisher_class + digest-pinned (the local-base drift check is
    reserved for the fuller ``syrvis images``). Never raises.
    """
    try:
        trust = load_trust_registry(home)
        images = image_updates.collect_pinned_images(Path(home) if home else None)
        recs = [cheap_provenance(e["image"], trust=trust, inspect=False) for e in images]
        return {
            "count": len(recs),
            "trusted": sum(1 for r in recs if r.trust == "ok"),
            "attention": sum(1 for r in recs if r.trust != "ok"),
        }
    except Exception:  # noqa: BLE001 - status must never fail on the trust summary
        return {"count": 0, "trusted": 0, "attention": 0}
