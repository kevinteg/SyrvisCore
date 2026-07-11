"""/api/config, /api/versions, /api/info, /api/services contracts."""


def test_config_is_redacted(client):
    body = client.get("/api/config").json()
    assert body["domain"] == "example.com"
    assert body["values"]["CLOUDFLARE_TUNNEL_TOKEN"] == "****"
    assert body["values"]["PORTAINER_ADMIN_PASSWORD"] == "****"
    assert body["values"]["DOMAIN"] == "example.com"
    assert body["enabled_components"]["cloudflared"] is True
    # the raw secret never appears
    assert "tok-secret" not in str(body)


def test_versions(client):
    body = client.get("/api/versions").json()
    assert body["active_version"] == "0.2.0"
    assert "0.2.0" in body["versions"]


def test_info(client):
    body = client.get("/api/info").json()
    assert body["active_version"] == "0.2.0"
    assert body["setup_complete"] is True
    assert "dashboard_version" in body


def test_services_degrade_without_docker(client):
    body = client.get("/api/services").json()
    assert "core" in body and "layer2" in body
    # no docker daemon in tests → core reports an error, never 500
    assert body["core"]["items"] == []
    assert "error" in body["core"]
    assert isinstance(body["layer2"]["items"], list)
