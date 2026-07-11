"""/api/health contract — never 500s even when every component is unreachable."""

from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app
from syrviscore_dashboard.probes import COMPONENTS


def _unreachable_client(make_settings):
    # Point component URLs at a fast-refusing loopback port so probes fail
    # immediately (no DNS lookups of "traefik"/"portainer" in tests).
    s = make_settings(
        traefik_url="http://127.0.0.1:1",
        portainer_url="http://127.0.0.1:1",
        cloudflared_url="http://127.0.0.1:1",
    )
    return TestClient(create_app(s))


def test_health_snapshot_shape(make_settings):
    client = _unreachable_client(make_settings)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"generated_at", "overall", "healthy", "components"}
    # every registered probe reported something
    assert set(body["components"]) == set(COMPONENTS)
    for comp in body["components"].values():
        assert comp["status"] in {"ok", "degraded", "down", "not_configured"}


def test_health_component_and_404(make_settings):
    client = _unreachable_client(make_settings)
    assert client.get("/api/health/config").status_code == 200
    assert client.get("/api/health/nope").status_code == 404


def test_health_is_cached(make_settings):
    client = _unreachable_client(make_settings)
    first = client.get("/api/health").json()["generated_at"]
    second = client.get("/api/health").json()["generated_at"]
    assert first == second  # served from the TTL cache
    forced = client.get("/api/health?refresh=true").json()["generated_at"]
    assert forced != first
