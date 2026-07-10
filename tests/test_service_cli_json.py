"""
Contract tests for the --json output added to `syrvis status` and
`syrvis service list` (the machine-readable surface the MCP layer consumes).

Docker/service managers are faked — no docker, no NAS.
"""

import json

import pytest
from click.testing import CliRunner

from syrviscore import cli as service_cli
from syrviscore.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestStatusJson:
    def test_status_json_shape(self, runner, monkeypatch):
        class FakeManager:
            def get_container_status(self):
                return {
                    "traefik": {
                        "name": "traefik",
                        "status": "running",
                        "uptime": "2 hours ago",
                        "image": "traefik:v3.0.0",
                    }
                }

        monkeypatch.setattr(service_cli, "DockerManager", lambda: FakeManager())
        monkeypatch.setattr(service_cli, "get_active_version", lambda: "0.1.21")

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["version"] == "0.1.21"
        assert data["services"]["traefik"]["status"] == "running"

    def test_status_json_error_is_structured(self, runner, monkeypatch):
        from syrviscore.docker_manager import DockerConnectionError

        def boom():
            raise DockerConnectionError("daemon down")

        monkeypatch.setattr(service_cli, "DockerManager", boom)
        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "daemon down"


class TestServiceListJson:
    def test_service_list_json_shape(self, runner, monkeypatch):
        import syrviscore.service_manager as sm

        class FakeServiceManager:
            def __init__(self, *a, **k):
                pass

            def list(self):
                return [
                    {
                        "name": "gollum",
                        "version": "1.0.0",
                        "status": "running",
                        "url": "https://wiki.example.com",
                        "description": "wiki",
                    }
                ]

        monkeypatch.setattr(sm, "ServiceManager", FakeServiceManager)
        result = runner.invoke(cli, ["service", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["services"][0]["name"] == "gollum"
        assert data["services"][0]["status"] == "running"

    def test_service_list_json_empty(self, runner, monkeypatch):
        import syrviscore.service_manager as sm

        class FakeServiceManager:
            def __init__(self, *a, **k):
                pass

            def list(self):
                return []

        monkeypatch.setattr(sm, "ServiceManager", FakeServiceManager)
        result = runner.invoke(cli, ["service", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"services": []}
