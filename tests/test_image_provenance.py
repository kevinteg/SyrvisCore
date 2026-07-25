"""Tests for image provenance/freshness (syrvis images)."""

import pytest

from syrviscore import image_provenance as ip


# ---------------------------------------------------------------------------
# Trust registry
# ---------------------------------------------------------------------------
def test_bundled_registry_loads_and_covers_core():
    reg = ip.load_trust_registry(home=None)  # bundled only
    assert reg, "bundled image_trust.yaml should load"
    # canonical keys (as parse_image_ref produces)
    assert reg["docker.io/library/traefik"]["publisher_class"] == "official"
    assert reg["ghcr.io/kevinteg/docker-state-exporter"]["publisher_class"] == "trusted-org"


def test_every_registry_key_parses_and_has_valid_class():
    reg = ip.load_trust_registry(home=None)
    for key, entry in reg.items():
        ref = ip.parse_image_ref(key + ":x")  # keys are registry/repository
        assert "{}/{}".format(ref.registry, ref.repository) == key, key
        assert entry["publisher_class"] in ip.ALL_CLASSES, (key, entry["publisher_class"])


# ---------------------------------------------------------------------------
# cheap_provenance — the verdict logic (no network, no docker: inspect=False)
# ---------------------------------------------------------------------------
def _reg():
    return ip.load_trust_registry(home=None)


def test_trusted_digest_pinned_is_ok():
    p = ip.cheap_provenance(
        "ghcr.io/kevinteg/docker-state-exporter@sha256:" + "a" * 64,
        trust=_reg(),
        inspect=False,
    )
    assert p.publisher_class == "trusted-org"
    assert p.trust_source == "registry"
    assert p.digest_pinned is True
    assert p.trust == "ok"


def test_official_but_tag_pinned_is_warn():
    p = ip.cheap_provenance("traefik:v3.6.5", trust=_reg(), inspect=False)
    assert p.publisher_class == "official"
    assert p.digest_pinned is False
    assert p.trust == "warn"  # tag pins are mutable
    assert any("digest" in n for n in p.notes)


def test_community_publisher_is_warn_even_when_digest_pinned():
    p = ip.cheap_provenance("louislam/uptime-kuma@sha256:" + "b" * 64, trust=_reg(), inspect=False)
    assert p.publisher_class == "community"
    assert p.trust == "warn"
    assert any("publisher class" in n for n in p.notes)


def test_unknown_image_derives_publisher():
    # A ghcr image not in the registry → derived community (GHCR carries no signal).
    p = ip.cheap_provenance(
        "ghcr.io/someone/random@sha256:" + "c" * 64, trust=_reg(), inspect=False
    )
    assert p.trust_source == "derived"
    assert p.publisher_class == "community"
    assert p.trust == "warn"
    # A bare Docker Hub official-namespace image derives official.
    p2 = ip.cheap_provenance("redis:7@x" if False else "redis:7", trust=_reg(), inspect=False)
    assert p2.repository == "library/redis"
    assert p2.publisher_class == "official"


def test_base_drift_detected_from_local_label(monkeypatch):
    # traefik expects alpine; pretend the local image labels a debian base.
    monkeypatch.setattr(
        ip,
        "_local_inspect",
        lambda image: {
            "created": "2026-01-01T00:00:00Z",
            "base_from_label": "debian:12",
            "resolved_digest": "",
        },
    )
    p = ip.cheap_provenance("traefik@sha256:" + "d" * 64, trust=_reg(), inspect=True)
    assert p.base_drift is True
    assert p.trust == "warn"
    assert any("base drift" in n for n in p.notes)


def test_created_epoch_is_untrusted():
    assert ip._created_is_trusted("1970-01-01T00:00:00Z") is False
    assert ip._created_is_trusted("2026-07-01T12:00:00Z") is True
    assert ip._created_is_trusted(None) is None


def test_unparseable_ref_degrades():
    p = ip.cheap_provenance("", trust=_reg(), inspect=False)
    assert p.trust == "warn"
    assert any("unparseable" in n for n in p.notes)


# ---------------------------------------------------------------------------
# status_summary + build_report (cheap tier; stub the image collection)
# ---------------------------------------------------------------------------
@pytest.fixture
def fleet(monkeypatch):
    fleet = [
        {"kind": "core", "name": "traefik", "image": "traefik:v3.6.5"},  # official, tag → warn
        {
            "kind": "service",
            "name": "exporter",
            "image": "ghcr.io/kevinteg/docker-state-exporter@sha256:" + "a" * 64,
        },  # trusted + digest → ok
        {
            "kind": "service",
            "name": "kuma",
            "image": "louislam/uptime-kuma@sha256:" + "b" * 64,
        },  # community → warn
    ]
    monkeypatch.setattr(ip.image_updates, "collect_pinned_images", lambda home=None: fleet)
    return fleet


def test_status_summary_counts(fleet):
    s = ip.status_summary(home=None)
    assert s["count"] == 3
    assert s["trusted"] == 1  # only the digest-pinned trusted-org one
    assert s["attention"] == 2


def test_build_report_cheap_tier_shape(fleet):
    r = ip.build_report(home=None, refresh=False, inspect=False)
    assert r["count"] == 3
    assert r["trusted"] == 1 and r["attention"] == 2
    assert r["heavy_fresh"] is False
    imgs = {i["name"]: i for i in r["images"]}
    assert imgs["exporter"]["trust"] == "ok"
    assert imgs["traefik"]["trust"] == "warn"
    # heavy fields are present (None) even without a refresh
    assert "update_available" in imgs["kuma"] and imgs["kuma"]["update_available"] is None
