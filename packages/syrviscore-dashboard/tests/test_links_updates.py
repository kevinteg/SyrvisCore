"""Launcher links + update-check endpoints."""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app

_RELEASES = "https://api.github.com/repos/kevinteg/SyrvisCore/releases"


@pytest.fixture(autouse=True)
def _no_registry_calls(monkeypatch):
    """Stub the container-image update path so the version tests (which now also
    hit /api/updates' images section) never make real registry calls. The image
    section gets its own explicit tests below."""
    from syrviscore import image_updates as iu

    empty = {"count": 0, "update_count": 0, "images": [], "cached": True}
    monkeypatch.setattr(iu, "check_updates", lambda **kw: empty)
    monkeypatch.setattr(iu, "read_cached", lambda **kw: empty)


def test_links_primordial(client):
    body = client.get("/api/links").json()
    assert body["domain"] == "example.com"
    urls = {link["url"] for link in body["links"]}
    assert "https://portainer.example.com" in urls
    assert "https://traefik.example.com" in urls


def test_links_include_enabled_synology(make_settings, syrvis_home):
    (syrvis_home / "config" / ".env").write_text("DOMAIN=example.com\nSYNOLOGY_DSM_ENABLED=true\n")
    client = TestClient(create_app(make_settings()))
    links = client.get("/api/links").json()["links"]
    urls = {link["url"] for link in links}
    assert "https://dsm.example.com" in urls
    # a disabled Synology service is not linked
    assert "https://photos.example.com" not in urls


def test_updates_available(client):
    with respx.mock:
        respx.get(_RELEASES).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"tag_name": "v0.2.2", "prerelease": False, "draft": False},
                    {"tag_name": "v0.2.0", "prerelease": False, "draft": False},
                    {"tag_name": "manager-v0.3.0", "prerelease": False, "draft": False},
                ],
            )
        )
        body = client.get("/api/updates?refresh=true").json()
    assert body["current"] == "0.2.0"  # from the fixture manifest
    assert body["latest"] == "0.2.2"  # manager-* excluded
    assert body["update_available"] is True


def test_updates_up_to_date(client):
    with respx.mock:
        respx.get(_RELEASES).mock(
            return_value=httpx.Response(
                200, json=[{"tag_name": "v0.2.0", "prerelease": False, "draft": False}]
            )
        )
        body = client.get("/api/updates?refresh=true").json()
    assert body["latest"] == "0.2.0"
    assert body["update_available"] is False


def test_updates_github_unreachable(client):
    with respx.mock:
        respx.get(_RELEASES).mock(side_effect=httpx.ConnectError("no net"))
        body = client.get("/api/updates?refresh=true").json()
    assert body["latest"] is None
    assert body["update_available"] is False
    assert "error" in body


def test_updates_includes_image_section(client, monkeypatch):
    """The /api/updates response carries a structured images report alongside
    the platform-version fields."""
    from syrviscore import image_updates as iu

    report = {
        "count": 2,
        "update_count": 1,
        "cached": True,
        "images": [
            {
                "kind": "core",
                "name": "traefik",
                "image": "traefik:v3.6.5",
                "current": "v3.6.5",
                "latest": "v3.7.0",
                "update_available": True,
                "newer": ["v3.7.0"],
                "error": None,
            },
            {
                "kind": "service",
                "name": "web",
                "image": "nginx:1.25.3",
                "current": "1.25.3",
                "latest": "1.25.3",
                "update_available": False,
                "newer": [],
                "error": None,
            },
        ],
    }
    monkeypatch.setattr(iu, "read_cached", lambda **kw: report)
    with respx.mock:
        respx.get(_RELEASES).mock(
            return_value=httpx.Response(
                200, json=[{"tag_name": "v0.2.0", "prerelease": False, "draft": False}]
            )
        )
        body = client.get("/api/updates").json()
    assert body["images"]["update_count"] == 1
    assert body["images"]["images"][0]["name"] == "traefik"
    # top level still carries the platform-version fields (header badge reads them)
    assert "update_available" in body and "version" in body


def test_updates_refresh_triggers_image_recheck(client, monkeypatch):
    seen = {"refresh": None}

    from syrviscore import image_updates as iu

    def fake_check(**kw):
        seen["refresh"] = kw.get("refresh")
        return {"count": 0, "update_count": 0, "images": [], "cached": False}

    monkeypatch.setattr(iu, "check_updates", fake_check)
    with respx.mock:
        respx.get(_RELEASES).mock(return_value=httpx.Response(200, json=[{"tag_name": "v0.2.0"}]))
        client.get("/api/updates?refresh=true")
    assert seen["refresh"] is True  # refresh flows through to the image check
