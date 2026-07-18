"""Tests for the external-state report (syrvis stack hostnames)."""

import pytest

from syrviscore import hostnames, stack


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    return h


def _write_env(home, body):
    (home / "config" / ".env").write_text(body)


def _by_service(report):
    return {e["service"]: e for e in report["entries"]}


def test_primordial_always_internal(home):
    _write_env(home, "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n")
    report = hostnames.build_report()
    assert report["domain"] == "example.com"
    assert report["traefik_ip"] == "192.168.1.100"

    entries = _by_service(report)
    assert entries["portainer"]["hostname"] == "portainer.example.com"
    assert entries["portainer"]["exposure"] == "internal"
    assert entries["portainer"]["record"] == {
        "type": "A",
        "name": "portainer.example.com",
        "target": "192.168.1.100",
        "proxied": False,
        "note": "LAN DNS record pointing at Traefik",
    }
    assert entries["traefik"]["hostname"] == "traefik.example.com"


def test_dashboard_tunnel_exposure(home):
    _write_env(home, "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n")
    stack.set_enabled("dashboard", True, {"subdomain": "panel", "exposure": "tunnel"})

    entries = _by_service(hostnames.build_report())
    dash = entries["dashboard"]
    assert dash["hostname"] == "panel.example.com"
    assert dash["exposure"] == "tunnel"
    assert dash["access_required"] is True
    assert dash["record"]["type"] == "CNAME"
    assert dash["record"]["proxied"] is True


def test_synology_service_exposure_from_env(home):
    _write_env(
        home,
        "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n"
        "SYNOLOGY_PHOTOS_ENABLED=true\nSYNOLOGY_PHOTOS_EXPOSURE=tunnel\n"
        "SYNOLOGY_DSM_ENABLED=true\n",
    )
    entries = _by_service(hostnames.build_report())
    # photos declared tunnel; dsm defaults internal; unset services absent.
    assert entries["synology_photos"]["exposure"] == "tunnel"
    assert entries["synology_dsm"]["exposure"] == "internal"
    assert "synology_drive" not in entries


def test_layer2_service_included(home, monkeypatch):
    _write_env(home, "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n")
    monkeypatch.setenv("DOMAIN", "example.com")
    from syrviscore.service_manager import ServiceManager

    ServiceManager(syrvis_home=home).add_image(
        "cyberquill", "ghcr.io/acme/cyberquill:1.4.0", exposure="tunnel", port=8080, start=False
    )

    entries = _by_service(hostnames.build_report())
    cq = entries["cyberquill"]
    assert cq["kind"] == "service"
    assert cq["hostname"] == "cyberquill.example.com"
    assert cq["exposure"] == "tunnel"
    assert cq["record"]["type"] == "CNAME"


def test_synology_webdav_in_report(home):
    """Enabling SYNOLOGY_WEBDAV_ENABLED produces a files.<domain> internal A-record entry."""
    _write_env(
        home,
        "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n"
        "SYNOLOGY_WEBDAV_ENABLED=true\n",
    )
    entries = _by_service(hostnames.build_report())
    assert "synology_webdav" in entries, "synology_webdav should appear when SYNOLOGY_WEBDAV_ENABLED=true"
    webdav = entries["synology_webdav"]
    assert webdav["hostname"] == "files.example.com"
    assert webdav["subdomain"] == "files"
    assert webdav["kind"] == "synology"
    assert webdav["exposure"] == "internal"
    assert webdav["record"]["type"] == "A"
    assert webdav["record"]["target"] == "192.168.1.100"


def test_synology_webdav_absent_when_disabled(home):
    """When SYNOLOGY_WEBDAV_ENABLED is not set, synology_webdav must be absent."""
    _write_env(home, "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\n")
    entries = _by_service(hostnames.build_report())
    assert "synology_webdav" not in entries


def test_missing_config_degrades_gracefully(tmp_path, monkeypatch):
    # No SYRVIS_HOME resolvable -> empty report with an error, never an exception.
    monkeypatch.delenv("SYRVIS_HOME", raising=False)
    monkeypatch.setenv("DSM_SIM_ACTIVE", "0")
    report = hostnames.build_report(env_path=str(tmp_path / "nope" / ".env"))
    assert report["entries"] == [] or "error" in report
