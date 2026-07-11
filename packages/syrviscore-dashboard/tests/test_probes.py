"""Unit tests for the component probes — real HTTP mocked with respx."""

import httpx
import pytest
import respx

from syrviscore_dashboard.probes import Status
from syrviscore_dashboard.probes.cloudflare_ddns import probe_ddns
from syrviscore_dashboard.probes.cloudflared import probe_cloudflared
from syrviscore_dashboard.probes.config import probe_config
from syrviscore_dashboard.probes.core import probe_core
from syrviscore_dashboard.probes.portainer import probe_portainer
from syrviscore_dashboard.probes.traefik import probe_traefik
from syrviscore_dashboard.settings import DashboardSettings


@pytest.fixture
def settings(syrvis_home, monkeypatch):
    monkeypatch.setenv("SYRVIS_HOME", str(syrvis_home))
    return DashboardSettings(syrvis_home=str(syrvis_home), dashboard_auth_mode="none")


# --- traefik ---------------------------------------------------------------


async def test_traefik_ok(settings):
    with respx.mock:
        respx.get("http://traefik:8080/ping").mock(return_value=httpx.Response(200, text="OK"))
        respx.get("http://traefik:8080/api/overview").mock(
            return_value=httpx.Response(
                200, json={"http": {"routers": {"total": 3}}, "features": {}}
            )
        )
        respx.get("http://traefik:8080/api/http/routers").mock(
            return_value=httpx.Response(200, json=[{"name": "portainer@docker"}])
        )
        async with httpx.AsyncClient() as http:
            result = await probe_traefik(settings, http)
    assert result.status == Status.OK
    assert result.extra["router_names"] == ["portainer@docker"]


async def test_traefik_down_on_connect_error(settings):
    with respx.mock:
        respx.get("http://traefik:8080/ping").mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as http:
            result = await probe_traefik(settings, http)
    assert result.status == Status.DOWN


async def test_traefik_degraded_when_api_fails(settings):
    with respx.mock:
        respx.get("http://traefik:8080/ping").mock(return_value=httpx.Response(200, text="OK"))
        respx.get("http://traefik:8080/api/overview").mock(return_value=httpx.Response(500))
        respx.get("http://traefik:8080/api/http/routers").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as http:
            result = await probe_traefik(settings, http)
    assert result.status == Status.DEGRADED


# --- portainer -------------------------------------------------------------


async def test_portainer_ok(settings):
    with respx.mock:
        respx.get("http://portainer:9000/api/status").mock(
            return_value=httpx.Response(200, json={"Version": "2.33.6", "InstanceID": "abc"})
        )
        async with httpx.AsyncClient() as http:
            result = await probe_portainer(settings, http)
    assert result.status == Status.OK
    assert result.extra["version"] == "2.33.6"


async def test_portainer_down(settings):
    with respx.mock:
        respx.get("http://portainer:9000/api/status").mock(side_effect=httpx.ConnectError("x"))
        async with httpx.AsyncClient() as http:
            result = await probe_portainer(settings, http)
    assert result.status == Status.DOWN


# --- cloudflared -----------------------------------------------------------


async def test_cloudflared_ok(settings):
    # fixture .env has CLOUDFLARE_TUNNEL_TOKEN set → component is "configured"
    with respx.mock:
        respx.get("http://cloudflared:20241/ready").mock(
            return_value=httpx.Response(200, json={"readyConnections": 4, "connectorId": "c1"})
        )
        async with httpx.AsyncClient() as http:
            result = await probe_cloudflared(settings, http)
    assert result.status == Status.OK
    assert result.extra["readyConnections"] == 4


async def test_cloudflared_degraded_when_metrics_unreachable(settings):
    with respx.mock:
        respx.get("http://cloudflared:20241/ready").mock(side_effect=httpx.ConnectError("x"))
        async with httpx.AsyncClient() as http:
            result = await probe_cloudflared(settings, http)
    assert result.status == Status.DEGRADED


async def test_cloudflared_not_configured(settings, syrvis_home):
    # remove the tunnel token → not configured (never DOWN)
    (syrvis_home / "config" / ".env").write_text("DOMAIN=example.com\n")
    async with httpx.AsyncClient() as http:
        result = await probe_cloudflared(settings, http)
    assert result.status == Status.NOT_CONFIGURED


# --- cloudflare ddns -------------------------------------------------------


async def test_ddns_not_configured(settings):
    # fixture .env has no CLOUDFLARE_API_TOKEN
    async with httpx.AsyncClient() as http:
        result = await probe_ddns(settings, http)
    assert result.status == Status.NOT_CONFIGURED


async def test_ddns_in_sync(settings, syrvis_home):
    (syrvis_home / "config" / ".env").write_text(
        "DOMAIN=example.com\n"
        "CLOUDFLARE_API_TOKEN=cf-token\n"
        "CLOUDFLARE_DDNS_RECORDS=home.example.com\n"
    )
    with respx.mock:
        respx.get("https://api.ipify.org").mock(
            return_value=httpx.Response(200, text="203.0.113.7")
        )
        respx.get("https://api.cloudflare.com/client/v4/zones").mock(
            return_value=httpx.Response(200, json={"result": [{"id": "z1", "name": "example.com"}]})
        )
        respx.get("https://api.cloudflare.com/client/v4/zones/z1/dns_records").mock(
            return_value=httpx.Response(200, json={"result": [{"content": "203.0.113.7"}]})
        )
        async with httpx.AsyncClient() as http:
            result = await probe_ddns(settings, http)
    assert result.status == Status.OK
    assert result.extra["public_ip"] == "203.0.113.7"
    assert result.extra["records"][0]["in_sync"] is True


async def test_ddns_out_of_sync(settings, syrvis_home):
    (syrvis_home / "config" / ".env").write_text(
        "CLOUDFLARE_API_TOKEN=cf-token\nCLOUDFLARE_DDNS_RECORDS=home.example.com\n"
    )
    with respx.mock:
        respx.get("https://api.ipify.org").mock(
            return_value=httpx.Response(200, text="203.0.113.7")
        )
        respx.get("https://api.cloudflare.com/client/v4/zones").mock(
            return_value=httpx.Response(200, json={"result": [{"id": "z1", "name": "example.com"}]})
        )
        respx.get("https://api.cloudflare.com/client/v4/zones/z1/dns_records").mock(
            return_value=httpx.Response(200, json={"result": [{"content": "198.51.100.1"}]})
        )
        async with httpx.AsyncClient() as http:
            result = await probe_ddns(settings, http)
    assert result.status == Status.DEGRADED
    assert result.extra["records"][0]["in_sync"] is False


# --- config ----------------------------------------------------------------


async def test_config_probe_ok(settings):
    result = await probe_config(settings)
    assert result.status == Status.OK
    assert result.extra["domain"] == "example.com"
    assert result.extra["enabled_components"]["cloudflared"] is True


# --- core (no docker daemon here → graceful DOWN) --------------------------


async def test_core_down_without_docker(settings):
    result = await probe_core(settings)
    assert result.status == Status.DOWN
    assert "docker" in result.detail.lower()
