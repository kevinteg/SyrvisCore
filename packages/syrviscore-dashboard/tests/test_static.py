"""The built SPA is served, with client-route fallback and API passthrough."""

from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app


def _spa_client(make_settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "assets" / "app.js").write_text("// js")
    return TestClient(create_app(make_settings(dashboard_static_dir=str(dist))))


def test_spa_root_and_fallback(make_settings, tmp_path):
    client = _spa_client(make_settings, tmp_path)
    assert client.get("/").text.strip() == "<html>spa</html>"
    # unknown client route falls back to index.html
    assert client.get("/services").text.strip() == "<html>spa</html>"
    # a real asset is served directly
    assert client.get("/assets/app.js").status_code == 200


def test_api_not_shadowed_by_spa(make_settings, tmp_path):
    client = _spa_client(make_settings, tmp_path)
    assert client.get("/api/info").status_code == 200
    assert client.get("/api/nope").status_code == 404  # not rewritten to index.html
    assert client.get("/healthz").json()["status"] == "ok"


def test_no_static_dir_is_noop(client):
    # default fixture has no static dir → root is not a SPA route
    assert client.get("/").status_code == 404
