"""Liveness endpoint is unauthenticated and always returns ok."""


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "syrviscore-dashboard"
    assert "version" in body
