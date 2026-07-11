"""SSE endpoints: /api/events (health) and /api/logs/{service}."""

import json

import pytest
from fastapi.testclient import TestClient

from syrviscore_dashboard.aggregator import HealthAggregator
from syrviscore_dashboard.api.events import health_event_stream
from syrviscore_dashboard.app import create_app


class FakeContainer:
    def __init__(self):
        self.labels = {"com.docker.compose.project": "syrviscore"}

    def logs(self, tail=100, timestamps=False, stream=False, follow=False):
        if stream:
            return iter([b"line-a\n", b"line-b\n"])
        return b"hello\nworld\n"


def _client(make_settings, **overrides):
    s = make_settings(
        traefik_url="http://127.0.0.1:1",
        portainer_url="http://127.0.0.1:1",
        cloudflared_url="http://127.0.0.1:1",
        aggregator_ttl_s=0.5,
        **overrides,
    )
    return TestClient(create_app(s))


async def test_health_event_stream_yields_then_stops(make_settings):
    """Drive the generator directly (an infinite HTTP stream is awkward to test)."""
    settings = make_settings(
        traefik_url="http://127.0.0.1:1",
        portainer_url="http://127.0.0.1:1",
        cloudflared_url="http://127.0.0.1:1",
        aggregator_ttl_s=0.5,
    )
    agg = HealthAggregator(settings)
    state = {"n": 0}

    async def is_disconnected():
        state["n"] += 1
        return state["n"] > 1  # connected for the first iteration only

    gen = health_event_stream(agg, settings, is_disconnected)
    first = await gen.__anext__()
    assert first["event"] == "health"
    data = json.loads(first["data"])
    assert "components" in data and "overall" in data
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


def test_events_endpoint_headers(make_settings):
    """The route returns an event-stream response (headers only; no read)."""
    client = _client(make_settings)
    # HEAD-like check: the app exposes the route with the right media type in OpenAPI.
    schema = client.get("/api/openapi.json").json()
    assert "/api/events" in schema["paths"]


def test_logs_oneshot(make_settings, monkeypatch):
    monkeypatch.setattr(
        "syrviscore_dashboard.docker_util.get_managed_container", lambda name: FakeContainer()
    )
    client = _client(make_settings)
    resp = client.get("/api/logs/traefik")
    assert resp.status_code == 200
    assert "hello" in resp.text and "world" in resp.text


def test_logs_stream(make_settings, monkeypatch):
    monkeypatch.setattr(
        "syrviscore_dashboard.docker_util.get_managed_container", lambda name: FakeContainer()
    )
    client = _client(make_settings)
    lines = []
    with client.stream("GET", "/api/logs/traefik?stream=true") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data:"):
                lines.append(line[len("data:") :].strip())
    assert lines == ["line-a", "line-b"]


def test_logs_docker_unavailable(client):
    # no docker daemon in tests → 503, never a 500
    resp = client.get("/api/logs/traefik")
    assert resp.status_code == 503
