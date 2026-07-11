"""Container-safe management: core lifecycle, L2 gating, and SSH actions."""

import pytest
from fastapi.testclient import TestClient

from syrviscore_dashboard import docker_util, manage
from syrviscore_dashboard.app import create_app


class FakeContainer:
    def __init__(self):
        self.calls = []
        self.labels = {"com.docker.compose.project": "syrviscore"}

    def start(self):
        self.calls.append("start")

    def stop(self, timeout=10):
        self.calls.append("stop")

    def restart(self, timeout=10):
        self.calls.append("restart")


class FakeServiceManager:
    def __init__(self):
        self.calls = []

    def start(self, name):
        self.calls.append(("start", name))
        return True, "started {}".format(name)

    def stop(self, name):
        self.calls.append(("stop", name))
        return True, "stopped {}".format(name)

    def update(self, name):
        self.calls.append(("update", name))
        return True, "updated {}".format(name)

    def add(self, source, start=True):
        self.calls.append(("add", source, start))
        return True, "added {}".format(source)

    def remove(self, name, purge=False):
        self.calls.append(("remove", name, purge))
        return True, "removed {}".format(name)


# --- core lifecycle (unit) -------------------------------------------------


def test_core_lifecycle_restart(monkeypatch):
    fake = FakeContainer()
    monkeypatch.setattr(docker_util, "get_managed_container", lambda name: fake)
    ok, msg = manage.core_lifecycle("traefik", "restart")
    assert ok and fake.calls == ["restart"]
    assert "restarted" in msg


def test_core_lifecycle_rejects_unknown_service(monkeypatch):
    monkeypatch.setattr(docker_util, "get_managed_container", lambda name: FakeContainer())
    with pytest.raises(docker_util.NotManaged):
        manage.core_lifecycle("some-other-container", "restart")


def test_core_lifecycle_rejects_bad_action(monkeypatch):
    monkeypatch.setattr(docker_util, "get_managed_container", lambda name: FakeContainer())
    with pytest.raises(ValueError):
        manage.core_lifecycle("traefik", "nuke")


# --- core lifecycle (API) --------------------------------------------------


def test_core_action_api(client, monkeypatch):
    fake = FakeContainer()
    monkeypatch.setattr(docker_util, "get_managed_container", lambda name: fake)
    resp = client.post("/api/core/portainer/restart")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert fake.calls == ["restart"]


def test_core_action_unknown_service_403(client):
    assert client.post("/api/core/randomthing/start").status_code == 403


def test_core_action_bad_action_400(client, monkeypatch):
    monkeypatch.setattr(docker_util, "get_managed_container", lambda name: FakeContainer())
    assert client.post("/api/core/traefik/frobnicate").status_code == 400


def test_core_action_docker_unavailable_503(client):
    # real docker_util path, no daemon → 503
    assert client.post("/api/core/traefik/restart").status_code == 503


# --- layer 2 gating --------------------------------------------------------


def test_layer2_disabled_by_default(client):
    assert client.post("/api/services/gollum/start").status_code == 403
    assert client.post("/api/services", json={"source": "x"}).status_code == 403
    assert client.delete("/api/services/gollum").status_code == 403


def test_layer2_enabled(make_settings, monkeypatch):
    fake = FakeServiceManager()
    monkeypatch.setattr(manage, "_service_manager", lambda: fake)
    client = TestClient(create_app(make_settings(enable_l2_mutations=True)))

    assert client.post("/api/services/gollum/start").json() == {
        "ok": True,
        "message": "started gollum",
    }
    assert client.post("/api/services", json={"source": "https://x/y.git"}).json()["ok"] is True
    assert client.delete("/api/services/gollum?purge=true").json()["ok"] is True
    assert ("remove", "gollum", True) in fake.calls


# --- ssh actions -----------------------------------------------------------


def test_system_actions_list(client):
    body = client.get("/api/system/actions").json()
    ids = {a["id"] for a in body["actions"]}
    assert {"setup", "verify-fix", "core-reconcile"} <= ids
    for action in body["actions"]:
        assert action["ssh_command"].startswith("ssh nas '")


def test_system_action_command(client):
    body = client.post("/api/system/actions/verify-fix").json()
    assert body["ssh_command"] == "ssh nas 'sudo syrvis verify --fix'"
    assert client.post("/api/system/actions/nope").status_code == 404
