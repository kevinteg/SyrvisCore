"""
Security tests for the Layer 2 service trust boundary.

A syrvis-service.yaml is attacker-controlled input from a third-party git
repo. These tests pin the guarantees from the Phase 3 audit fixes:
- C1: a service name cannot traverse out of its directories
- C2: volumes cannot mount arbitrary host paths or the docker socket
- unknown keys / unpinned images / privileged options are rejected
"""

import pytest

from syrviscore.service_schema import (
    ServiceDefinition,
    ServiceValidationError,
    validate_service_name,
)


class TestServiceNameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "../../../../usr/local/etc/rc.d/S99evil",
            "..",
            "foo/bar",
            "foo/../bar",
            "/etc/passwd",
            "UPPER",
            "has space",
            "trailing/",
            "",
            ".hidden",
            "a" * 65,
        ],
    )
    def test_malicious_names_rejected(self, name):
        with pytest.raises(ServiceValidationError):
            validate_service_name(name)

    @pytest.mark.parametrize("name", ["gollum", "home-assistant", "rag_db", "svc1"])
    def test_valid_names_accepted(self, name):
        assert validate_service_name(name) == name

    @pytest.mark.parametrize("name", ["traefik", "portainer", "cloudflared", "proxy"])
    def test_reserved_core_names_rejected(self, name):
        with pytest.raises(ServiceValidationError):
            validate_service_name(name)


def base_service(**overrides):
    data = {"name": "svc", "version": "1.0.0", "image": "nginx:1.27.0"}
    data.update(overrides)
    return data


class TestServiceDefinitionSecurity:
    def test_traversal_name_rejected_at_parse(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(name="../../evil"))

    def test_container_name_traversal_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(container_name="../../evil"))

    @pytest.mark.parametrize(
        "volume",
        [
            "/:/host:rw",
            "/etc:/etc:rw",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "../../../etc:/etc:rw",
            "../escape:/data:rw",
            "$HOME/x:/data:rw",
            "data:/container:xw",  # bad mode
            "onlyonefield",
        ],
    )
    def test_dangerous_volumes_rejected(self, volume):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(volumes=[volume]))

    @pytest.mark.parametrize(
        "volume",
        ["wiki:/wiki:rw", "subdir/data:/var/lib/app:ro", "conf:/etc/app"],
    )
    def test_safe_relative_volumes_accepted(self, volume):
        svc = ServiceDefinition.from_dict(base_service(volumes=[volume]))
        assert svc.volumes == [volume]

    def test_docker_sock_rejected_any_form(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(volumes=["/var/run/docker.sock:/sock"]))

    def test_unknown_keys_rejected(self):
        # privileged/cap_add/network_mode etc. would arrive as unknown keys
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(privileged=True))
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(cap_add=["SYS_ADMIN"]))

    def test_depends_on_rejected_as_unsupported(self):
        # Each service is its own compose project, so depends_on can never work;
        # it must fail loudly at parse time, not silently no-op at run time.
        with pytest.raises(ServiceValidationError, match="depends_on is not supported"):
            ServiceDefinition.from_dict(base_service(depends_on=["db"]))
        # An empty/absent depends_on remains valid.
        assert ServiceDefinition.from_dict(base_service(depends_on=[])).depends_on == []

    @pytest.mark.parametrize("image", ["nginx", "nginx:latest", "nginx:", "has space:1.0"])
    def test_unpinned_or_latest_image_rejected(self, image):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(image=image))

    def test_digest_pinned_image_accepted(self):
        digest = "nginx@sha256:" + "a" * 64
        svc = ServiceDefinition.from_dict(base_service(image=digest))
        assert svc.image == digest

    def test_bad_restart_policy_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(restart="always-ish"))

    def test_bad_env_entry_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(environment=["not-an-assignment"]))
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(environment=["1BAD=value"]))

    def test_bad_subdomain_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(
                base_service(traefik={"enabled": True, "subdomain": "not a domain", "port": 80})
            )

    def test_bad_port_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(
                base_service(traefik={"enabled": True, "subdomain": "wiki", "port": 99999})
            )


class TestComposeGenerationContainment:
    def _manager(self, tmp_path):
        from syrviscore.service_manager import ServiceManager

        return ServiceManager(syrvis_home=tmp_path)

    def test_compose_paths_stay_contained(self, tmp_path):
        mgr = self._manager(tmp_path)
        p = mgr._service_paths("gollum")
        assert p["service"] == tmp_path / "services" / "gollum"
        assert p["compose"] == tmp_path / "compose" / "gollum.yaml"

    def test_service_paths_reject_bad_name(self, tmp_path):
        mgr = self._manager(tmp_path)
        with pytest.raises(ServiceValidationError):
            mgr._service_paths("../../evil")

    def test_generated_compose_resolves_volumes_under_data(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        svc = ServiceDefinition.from_dict(base_service(name="gollum", volumes=["wiki:/wiki:rw"]))
        compose_path = mgr._generate_compose_file(svc)

        import yaml

        compose = yaml.safe_load(compose_path.read_text())
        vols = compose["services"]["gollum"]["volumes"]
        expected = str((tmp_path / "data" / "gollum" / "wiki").resolve())
        assert vols == [f"{expected}:/wiki:rw"]
        # no-new-privileges is always set for Layer 2 services
        assert compose["services"]["gollum"]["security_opt"] == ["no-new-privileges:true"]
        # no deprecated top-level version key
        assert "version" not in compose

    def test_project_name_isolated_per_service(self, tmp_path):
        mgr = self._manager(tmp_path)
        assert mgr._project_name("gollum") == "syrvis-gollum"


class TestElevationPreservesHome:
    def test_self_elevate_forwards_syrvis_home(self, monkeypatch):
        import syrviscore.privilege as privilege

        monkeypatch.setenv("SYRVIS_HOME", "/volume1/syrviscore")
        monkeypatch.setattr(privilege.shutil, "which", lambda _: "/usr/bin/sudo")

        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            captured["args"] = args

        monkeypatch.setattr(privilege.os, "execv", fake_execv)
        # click.echo is harmless; run it
        privilege.self_elevate("need root")

        assert captured["path"] == "/usr/bin/sudo"
        assert "SYRVIS_HOME=/volume1/syrviscore" in captured["args"]
        # SYRVIS_HOME must appear before the interpreter so sudo treats it as env
        home_idx = captured["args"].index("SYRVIS_HOME=/volume1/syrviscore")
        assert home_idx == 1


class TestSchemaV2Fields:
    """healthcheck / env_file / resources — audited, strictly sub-validated."""

    def test_healthcheck_valid(self):
        svc = ServiceDefinition.from_dict(
            base_service(
                healthcheck={
                    "test": ["CMD", "curl", "-f", "http://localhost:8080/healthz"],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                }
            )
        )
        assert svc.healthcheck["retries"] == 3

    @pytest.mark.parametrize(
        "hc",
        [
            {"test": "curl localhost"},  # not a list
            {"test": ["SHELL", "x"]},  # bad first token
            {"test": ["CMD", "x"], "interval": "30 seconds"},  # bad duration
            {"test": ["CMD", "x"], "retries": 0},  # out of range
            {"test": ["CMD", "x"], "disable": True},  # unknown key
        ],
    )
    def test_healthcheck_invalid_rejected(self, hc):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(healthcheck=hc))

    def test_env_file_relative_only(self):
        svc = ServiceDefinition.from_dict(base_service(env_file="secrets.env"))
        assert svc.env_file == "secrets.env"
        for bad in ("/etc/passwd", "../outside.env"):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(env_file=bad))

    def test_resources_valid_and_invalid(self):
        svc = ServiceDefinition.from_dict(base_service(resources={"cpus": "1.5", "memory": "512m"}))
        assert svc.resources == {"cpus": "1.5", "memory": "512m"}
        for bad in ({"cpus": "lots"}, {"memory": "512q"}, {"gpu": 1}, {}):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(resources=bad))

    def test_v2_fields_round_trip_to_dict(self):
        data = base_service(
            healthcheck={"test": ["CMD", "true"]},
            env_file="secrets.env",
            resources={"memory": "256m"},
        )
        svc = ServiceDefinition.from_dict(data)
        out = svc.to_dict()
        assert out["healthcheck"] == {"test": ["CMD", "true"]}
        assert out["env_file"] == "secrets.env"
        assert out["resources"] == {"memory": "256m"}
        # and the round-tripped dict re-validates
        ServiceDefinition.from_dict(out)
