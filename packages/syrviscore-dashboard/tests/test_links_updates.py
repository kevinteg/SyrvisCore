"""Launcher links + update-check endpoints."""

import httpx
import respx
from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app

_RELEASES = "https://api.github.com/repos/kevinteg/SyrvisCore/releases"


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
