"""Tests for the image-first Layer 2 path (add_image) and enable-time overrides."""

import pytest
import yaml

from syrviscore.service_manager import ServiceManager, _image_tag
from syrviscore.service_schema import ServiceDefinition, ServiceValidationError


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.setenv("DOMAIN", "example.com")
    return h


def _manager(home):
    return ServiceManager(syrvis_home=home)


class TestImageTag:
    @pytest.mark.parametrize(
        "image,expected",
        [
            ("ghcr.io/acme/cyberquill:1.4.0", "1.4.0"),
            ("nginx:1.27.0", "1.27.0"),
            ("ghcr.io/a/b@sha256:" + "0" * 64, "0.0.0"),
            ("registry:5000/a/b", "0.0.0"),
        ],
    )
    def test_image_tag(self, image, expected):
        assert _image_tag(image) == expected


class TestAddImage:
    def test_creates_manifest_and_routes(self, home):
        sm = _manager(home)
        ok, msg = sm.add_image(
            "cyberquill",
            "ghcr.io/acme/cyberquill:1.4.0",
            exposure="tunnel",
            port=8080,
            start=False,
        )
        assert ok, msg

        # Effective manifest persisted with the synthesized routing.
        manifest = home / "services" / "cyberquill" / "syrvis-service.yaml"
        assert manifest.exists()
        d = yaml.safe_load(manifest.read_text())
        assert d["image"] == "ghcr.io/acme/cyberquill:1.4.0"
        assert d["traefik"] == {
            "enabled": True,
            "subdomain": "cyberquill",
            "port": 8080,
            "exposure": "tunnel",
        }

        # Traefik dynamic config written under data/traefik/config/dynamic/.
        assert (home / "data" / "traefik" / "config" / "dynamic" / "cyberquill.yaml").exists()

        # list() surfaces exposure + subdomain + url.
        row = next(r for r in sm.list() if r["name"] == "cyberquill")
        assert row["exposure"] == "tunnel"
        assert row["subdomain"] == "cyberquill"
        assert row["url"] == "https://cyberquill.example.com"

    def test_subdomain_defaults_to_name(self, home):
        sm = _manager(home)
        ok, _ = sm.add_image("wiki", "ghcr.io/acme/wiki:2.0.0", start=False)
        assert ok
        d = yaml.safe_load((home / "services" / "wiki" / "syrvis-service.yaml").read_text())
        assert d["traefik"]["subdomain"] == "wiki"
        assert d["traefik"]["exposure"] == "internal"

    def test_reserved_name_rejected(self, home):
        ok, msg = _manager(home).add_image("traefik", "ghcr.io/a/b:1.0", start=False)
        assert not ok and "reserved" in msg.lower()

    def test_unpinned_image_rejected(self, home):
        ok, msg = _manager(home).add_image("svc", "nginx:latest", start=False)
        assert not ok and "latest" in msg.lower()

    def test_duplicate_rejected(self, home):
        sm = _manager(home)
        assert sm.add_image("svc", "ghcr.io/a/b:1.0", start=False)[0]
        ok, msg = sm.add_image("svc", "ghcr.io/a/b:1.0", start=False)
        assert not ok and "already exists" in msg


class TestApplyOverrides:
    def _svc(self):
        return ServiceDefinition.from_dict(
            {
                "name": "svc",
                "version": "1.0.0",
                "image": "nginx:1.27.0",
                "traefik": {"enabled": True, "subdomain": "orig", "port": 80},
            }
        )

    def test_override_subdomain_and_exposure(self):
        svc = self._svc()
        ServiceManager._apply_overrides(svc, "custom", "tunnel")
        assert svc.traefik.subdomain == "custom"
        assert svc.traefik.exposure == "tunnel"

    def test_bad_subdomain_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceManager._apply_overrides(self._svc(), "Bad Sub", None)

    def test_bad_exposure_rejected(self):
        with pytest.raises(ValueError):
            ServiceManager._apply_overrides(self._svc(), None, "public")
