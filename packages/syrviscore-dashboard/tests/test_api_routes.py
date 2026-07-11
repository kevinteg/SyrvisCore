"""/api/routes contract — declared hostnames + live route health, never a 500.

The hostnames report is mocked at the library seam (``syrviscore.hostnames``)
and all HTTP (the Traefik API + the entrypoint probes) is mocked with respx,
so no docker daemon or network is needed.
"""

import httpx
import respx

import syrviscore.hostnames as hostnames_mod

_ROUTERS_URL = "http://traefik:8080/api/http/routers"
# The endpoint probes through Traefik's entrypoints, host derived from traefik_url.
_HTTPS_ENTRY = "https://traefik/"
_HTTP_ENTRY = "http://traefik/"


def _entry(service, kind, subdomain, exposure="internal", enabled=True):
    host = "{}.example.com".format(subdomain)
    return {
        "service": service,
        "kind": kind,
        "subdomain": subdomain,
        "hostname": host,
        "exposure": exposure,
        "enabled": enabled,
        "access_required": exposure == "tunnel",
        "record": {
            "type": "CNAME" if exposure == "tunnel" else "A",
            "name": host,
            "target": None if exposure == "tunnel" else "192.168.0.5",
            "proxied": exposure == "tunnel",
            "note": "",
        },
    }


def _mock_report(monkeypatch, entries, **extra):
    report = {"domain": "example.com", "traefik_ip": "192.168.0.5", "entries": entries}
    report.update(extra)
    monkeypatch.setattr(hostnames_mod, "build_report", lambda: report)


def _router_json(*hosts):
    return [{"name": h.split(".")[0] + "@docker", "rule": "Host(`{}`)".format(h)} for h in hosts]


def _https_by_host(responses):
    """Route the entrypoint probe by its Host header (that's how Traefik routes)."""

    def responder(request):
        return responses.get(request.headers.get("host", ""), httpx.Response(404))

    return responder


def test_routes_happy_path_mixed_kinds(client, monkeypatch):
    _mock_report(
        monkeypatch,
        [
            _entry("traefik", "core", "traefik"),
            _entry("synology_dsm", "synology", "dsm"),
            _entry("synology_photos", "synology", "photos"),
            _entry("cyberquill", "service", "cyberquill", exposure="tunnel"),
            _entry("ghost", "service", "ghost"),
        ],
    )
    with respx.mock:
        respx.get(_ROUTERS_URL).mock(
            return_value=httpx.Response(
                200,
                json=_router_json(
                    "traefik.example.com",
                    "dsm.example.com",
                    "photos.example.com",
                    "cyberquill.example.com",
                    # no router for ghost.example.com
                ),
            )
        )
        respx.get(_HTTPS_ENTRY).mock(
            side_effect=_https_by_host(
                {
                    "traefik.example.com": httpx.Response(200),
                    "dsm.example.com": httpx.Response(302),
                    "photos.example.com": httpx.Response(502),  # router up, backend failing
                    "cyberquill.example.com": httpx.Response(403),  # Access-gated == alive
                    # ghost falls through to the responder's 404
                }
            )
        )
        resp = client.get("/api/routes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "example.com"
    assert body["traefik_ip"] == "192.168.0.5"
    assert body["traefik_api_ok"] is True
    assert "error" not in body

    by_service = {e["service"]: e for e in body["entries"]}
    assert len(by_service) == 5

    # Backend answered -> ok, with the status code recorded.
    assert by_service["traefik"]["reachability"] == {
        "status": "ok",
        "http_code": 200,
        "detail": "HTTP 200 via traefik",
    }
    assert by_service["synology_dsm"]["reachability"]["status"] == "ok"
    assert by_service["synology_dsm"]["reachability"]["http_code"] == 302
    # 403 from an Access-gated tunnel app is ALIVE.
    assert by_service["cyberquill"]["reachability"]["status"] == "ok"
    assert by_service["cyberquill"]["reachability"]["http_code"] == 403
    assert by_service["cyberquill"]["exposure"] == "tunnel"
    # Router present but the backend 5xxs -> degraded.
    assert by_service["synology_photos"]["reachability"]["status"] == "degraded"
    assert by_service["synology_photos"]["router_present"] is True
    # No router in the live list AND the probe fails -> down.
    assert by_service["ghost"]["reachability"]["status"] == "down"
    assert by_service["ghost"]["router_present"] is False

    # Declared fields ride along untouched.
    assert by_service["synology_dsm"]["kind"] == "synology"
    assert by_service["ghost"]["hostname"] == "ghost.example.com"
    assert by_service["traefik"]["router_present"] is True


def test_routes_http_fallback_redirect_is_degraded(client, monkeypatch):
    # https transport dead, but the http entrypoint 308s (https-redirect
    # middleware): the router is live even though the backend is unproven.
    _mock_report(monkeypatch, [_entry("portainer", "core", "portainer")])
    with respx.mock:
        respx.get(_ROUTERS_URL).mock(
            return_value=httpx.Response(200, json=_router_json("portainer.example.com"))
        )
        respx.get(_HTTPS_ENTRY).mock(side_effect=httpx.ConnectError("tls broken"))
        respx.get(_HTTP_ENTRY).mock(return_value=httpx.Response(308))
        body = client.get("/api/routes").json()

    reach = body["entries"][0]["reachability"]
    assert reach["status"] == "degraded"
    assert reach["http_code"] == 308


def test_routes_traefik_api_down_yields_unknowns(client, monkeypatch):
    _mock_report(
        monkeypatch,
        [_entry("traefik", "core", "traefik"), _entry("synology_dsm", "synology", "dsm")],
    )
    with respx.mock:
        respx.get(_ROUTERS_URL).mock(side_effect=httpx.ConnectError("refused"))
        respx.get(_HTTPS_ENTRY).mock(side_effect=httpx.ConnectError("refused"))
        respx.get(_HTTP_ENTRY).mock(side_effect=httpx.ConnectError("refused"))
        resp = client.get("/api/routes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["traefik_api_ok"] is False
    assert "note" in body
    assert len(body["entries"]) == 2
    for entry in body["entries"]:
        assert entry["reachability"]["status"] == "unknown"
        assert entry["reachability"]["http_code"] is None
        assert entry["router_present"] is False


def test_routes_config_unreadable_degrades_to_error(client, monkeypatch):
    monkeypatch.setattr(
        hostnames_mod,
        "build_report",
        lambda: {"domain": None, "traefik_ip": None, "entries": [], "error": "SYRVIS_HOME gone"},
    )
    with respx.mock:
        respx.get(_ROUTERS_URL).mock(side_effect=httpx.ConnectError("refused"))
        resp = client.get("/api/routes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["error"] == "SYRVIS_HOME gone"
    assert body["domain"] is None


def test_routes_synology_marked_unmanaged(client, monkeypatch):
    # We route Synology services but do NOT manage them: managed must be False
    # for kind=synology and True for everything else.
    _mock_report(
        monkeypatch,
        [
            _entry("traefik", "core", "traefik"),
            _entry("synology_dsm", "synology", "dsm"),
            _entry("cyberquill", "service", "cyberquill", exposure="tunnel"),
        ],
    )
    with respx.mock:
        respx.get(_ROUTERS_URL).mock(return_value=httpx.Response(200, json=[]))
        respx.get(_HTTPS_ENTRY).mock(return_value=httpx.Response(200))
        body = client.get("/api/routes").json()

    managed = {e["service"]: e["managed"] for e in body["entries"]}
    assert managed == {"traefik": True, "synology_dsm": False, "cyberquill": True}
