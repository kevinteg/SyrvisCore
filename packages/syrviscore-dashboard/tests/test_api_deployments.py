"""Deployment history + runstate views, and the gated rollback action."""

from fastapi.testclient import TestClient

from syrviscore import deployments, lifecycle
from syrviscore.service_schema import ServiceDefinition
from syrviscore_dashboard import manage
from syrviscore_dashboard.app import create_app


def _record(home, name="blog", image="ghcr.io/a/blog:1.0", **kwargs):
    svc = ServiceDefinition.from_dict(
        {
            "name": name,
            "version": "1.0",
            "image": image,
            "environment": ["API_KEY=supersecret"],
        }
    )
    defaults = dict(action="deploy", trigger="cli", outcome="success")
    defaults.update(kwargs)
    return deployments.record_service_deploy(home, svc, **defaults)


class FakeServiceManager:
    def __init__(self):
        self.calls = []

    def rollback(self, name, to=None):
        self.calls.append(("rollback", name, to))
        return True, "rolled back {} to {}".format(name, to)


# --- history ----------------------------------------------------------------


def test_deployments_lists_redacted_history(client, syrvis_home):
    _record(syrvis_home)
    _record(syrvis_home, image="ghcr.io/a/blog:2.0")

    body = client.get("/api/deployments").json()
    records = body["workloads"]["blog"]
    assert [r["revision"] for r in records] == [2, 1]
    assert records[0]["previous_image"] is None  # recorder wasn't told; display only
    assert records[0]["env_names"] == ["API_KEY"]
    # Values are masked at the source — the secret never crosses this API.
    assert "supersecret" not in client.get("/api/deployments").text


def test_deployments_workload_filter_and_core(client, syrvis_home):
    _record(syrvis_home)
    deployments.record_core_apply(
        syrvis_home,
        pins={"traefik": "traefik:v3.6.5"},
        core_enabled=["traefik"],
        trigger="cli",
    )
    body = client.get("/api/deployments", params={"workload": "blog"}).json()
    assert set(body["workloads"]) == {"blog"}
    body = client.get("/api/deployments").json()
    assert "@core" in body["workloads"]


def test_deployments_empty_home_is_not_an_error(client):
    body = client.get("/api/deployments").json()
    assert body["workloads"] == {} and body["invalid"] == []


# --- runstate ---------------------------------------------------------------


def test_runstate_active_then_halted(client, syrvis_home):
    assert client.get("/api/runstate").json() == {"state": "active"}
    lifecycle.write_halted(
        syrvis_home, "ups", [{"name": "blog", "scope": "service", "state": "running"}]
    )
    body = client.get("/api/runstate").json()
    assert body["state"] == "halted"
    assert body["reason"] == "ups"
    assert body["resume_on_boot"] is True


# --- rollback (gated mutation) ----------------------------------------------


def test_rollback_disabled_is_403(client):
    resp = client.post("/api/services/blog/rollback", json={"to": 1})
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]


def test_rollback_routes_before_generic_action(make_settings, monkeypatch):
    fake = FakeServiceManager()
    monkeypatch.setattr(manage, "_service_manager", lambda: fake)
    client = TestClient(create_app(make_settings(enable_l2_mutations=True)))

    resp = client.post("/api/services/blog/rollback", json={"to": 3})
    assert resp.status_code == 200, resp.text
    assert fake.calls == [("rollback", "blog", 3)]

    # No body -> previous successful revision (to=None).
    resp = client.post("/api/services/blog/rollback", json={})
    assert resp.status_code == 200
    assert fake.calls[-1] == ("rollback", "blog", None)


def test_ssh_actions_include_shutdown_resume(client):
    actions = {a["id"]: a for a in client.get("/api/system/actions").json()["actions"]}
    assert "shutdown-instance" in actions
    assert "resume-instance" in actions
    assert "syrvis shutdown" in actions["shutdown-instance"]["ssh_command"]
