"""Tests for the service catalog (bundled + site-local templates)."""

import pytest
import yaml

from syrviscore.catalog import CatalogError, list_templates, resolve
from syrviscore.service_manager import ServiceManager


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    monkeypatch.setenv("DOMAIN", "example.com")
    return h


class TestResolve:
    def test_bundled_templates_resolve_and_validate(self):
        for name in ("gollum", "uptime-kuma", "homeassistant"):
            svc = resolve(name)
            assert svc.name == name
            assert svc.image and ":latest" not in svc.image
            assert svc.traefik.enabled and svc.traefik.subdomain

    def test_unknown_template_lists_available(self):
        with pytest.raises(CatalogError, match="gollum"):
            resolve("no-such-service")

    def test_site_template_overrides_bundled(self, home):
        site = home / "catalog"
        site.mkdir()
        (site / "gollum.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "gollum",
                    "version": "9.9.9",
                    "image": "gollum/gollum:v9.9.9",
                    "traefik": {"enabled": True, "subdomain": "wiki", "port": 4567},
                }
            )
        )
        assert resolve("gollum").image == "gollum/gollum:v9.9.9"

    def test_template_name_must_match_filename(self, home):
        site = home / "catalog"
        site.mkdir()
        (site / "impostor.yaml").write_text(
            yaml.safe_dump({"name": "other", "version": "1.0", "image": "a/b:1.0"})
        )
        with pytest.raises(CatalogError, match="must match its filename"):
            resolve("impostor")

    def test_invalid_template_fails_loudly(self, home):
        site = home / "catalog"
        site.mkdir()
        (site / "bad.yaml").write_text(
            yaml.safe_dump({"name": "bad", "version": "1.0", "image": "nginx:latest"})
        )
        with pytest.raises(CatalogError, match="failed validation"):
            resolve("bad")


class TestListTemplates:
    def test_bundled_listed_with_metadata(self):
        entries = {e["name"]: e for e in list_templates()}
        assert entries["gollum"]["source"] == "bundled"
        assert entries["uptime-kuma"]["exposure"] == "tunnel"

    def test_broken_site_template_reported_not_hidden(self, home):
        site = home / "catalog"
        site.mkdir()
        (site / "broken.yaml").write_text("{not yaml: [")
        entries = {e["name"]: e for e in list_templates()}
        assert "error" in entries["broken"]


class TestAddFromCatalog:
    def test_installs_with_overrides(self, home):
        sm = ServiceManager(syrvis_home=home)
        ok, msg = sm.add_from_catalog(
            "gollum", subdomain="notes", exposure="tunnel", port=8080, start=False
        )
        assert ok, msg

        manifest = yaml.safe_load(
            (home / "services" / "gollum" / "syrvis-service.yaml").read_text()
        )
        assert manifest["traefik"]["subdomain"] == "notes"
        assert manifest["traefik"]["exposure"] == "tunnel"
        assert manifest["traefik"]["port"] == 8080
        assert (home / "compose" / "gollum.yaml").exists()

    def test_unknown_name_suggests_image_flag(self, home):
        ok, msg = ServiceManager(syrvis_home=home).add_from_catalog("nope", start=False)
        assert not ok
        assert "--image" in msg

    def test_duplicate_rejected(self, home):
        sm = ServiceManager(syrvis_home=home)
        assert sm.add_from_catalog("gollum", start=False)[0]
        ok, msg = sm.add_from_catalog("gollum", start=False)
        assert not ok and "already exists" in msg

    def test_bad_port_override_rejected(self, home):
        ok, msg = ServiceManager(syrvis_home=home).add_from_catalog(
            "gollum", port=99999, start=False
        )
        assert not ok and "port" in msg.lower()
        assert not (home / "services" / "gollum").exists()
