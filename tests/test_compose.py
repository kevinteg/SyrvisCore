"""
Tests for Docker Compose generation module.
"""

from pathlib import Path
from typing import Dict

import pytest
import yaml

from syrviscore.compose import ComposeGenerator, generate_compose_from_config


@pytest.fixture
def valid_config() -> Dict:
    """Sample valid build configuration."""
    return {
        "metadata": {
            "syrviscore_version": "0.0.1",
            "created_at": "2024-11-29T00:00:00Z",
            "created_by": "test",
        },
        "docker_images": {
            "traefik": {
                "image": "library/traefik",
                "tag": "v3.0.0",
                "full_image": "library/traefik:v3.0.0",
            },
            "portainer": {
                "image": "portainer/portainer-ce",
                "tag": "2.19.4",
                "full_image": "portainer/portainer-ce:2.19.4",
            },
            "cloudflared": {
                "image": "cloudflare/cloudflared",
                "tag": "2024.1.5",
                "full_image": "cloudflare/cloudflared:2024.1.5",
            },
        },
    }


@pytest.fixture
def config_without_cloudflared() -> Dict:
    """Sample config without Cloudflared."""
    return {
        "metadata": {
            "syrviscore_version": "0.0.1",
            "created_at": "2024-11-29T00:00:00Z",
            "created_by": "test",
        },
        "docker_images": {
            "traefik": {
                "image": "library/traefik",
                "tag": "v3.0.0",
                "full_image": "library/traefik:v3.0.0",
            },
            "portainer": {
                "image": "portainer/portainer-ce",
                "tag": "2.19.4",
                "full_image": "portainer/portainer-ce:2.19.4",
            },
        },
    }


@pytest.fixture
def network_env_vars(monkeypatch):
    """Set network environment variables."""
    monkeypatch.setenv("NETWORK_INTERFACE", "ovs_eth0")
    monkeypatch.setenv("NETWORK_SUBNET", "192.168.0.0/24")
    monkeypatch.setenv("NETWORK_GATEWAY", "192.168.0.1")
    monkeypatch.setenv("TRAEFIK_IP", "192.168.0.100")


@pytest.fixture
def temp_config_file(valid_config, tmp_path):
    """Create temporary config file."""
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(valid_config, f)
    return config_file


@pytest.fixture
def temp_config_without_cloudflared(config_without_cloudflared, tmp_path):
    """Create temporary config file without cloudflared."""
    config_file = tmp_path / "config-no-cf.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_without_cloudflared, f)
    return config_file


class TestComposeGenerator:
    """Test ComposeGenerator class."""

    def test_init(self):
        """Test initialization."""
        generator = ComposeGenerator("test/path/config.yaml")
        assert generator.config_path == Path("test/path/config.yaml")
        assert generator.build_config is None

    def test_load_config_success(self, temp_config_file, valid_config):
        """Test loading valid config file."""
        generator = ComposeGenerator(str(temp_config_file))
        loaded_config = generator.load_config()

        assert loaded_config == valid_config
        assert generator.build_config is not None
        assert "docker_images" in generator.build_config

    def test_load_config_file_not_found(self):
        """Test loading non-existent config file."""
        generator = ComposeGenerator("nonexistent.yaml")

        with pytest.raises(FileNotFoundError, match="Config file not found"):
            generator.load_config()

    def test_load_config_invalid_yaml(self, tmp_path):
        """Test loading invalid YAML file."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("{ invalid: yaml:: content")

        generator = ComposeGenerator(str(invalid_file))

        with pytest.raises(yaml.YAMLError):
            generator.load_config()

    def test_load_config_missing_docker_images(self, tmp_path):
        """Test loading config without docker_images section."""
        config = {"metadata": {"version": "0.1.0"}}
        config_file = tmp_path / "incomplete.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config, f)

        generator = ComposeGenerator(str(config_file))

        with pytest.raises(ValueError, match="missing docker_images section"):
            generator.load_config()

    def test_generate_traefik_service(self, temp_config_file, network_env_vars):
        """Test Traefik service generation with macvlan."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        network_config = generator._get_network_config_from_env()
        service = generator._generate_traefik_service(network_config)

        assert service["image"] == "library/traefik:v3.0.0"
        assert service["container_name"] == "traefik"
        assert service["restart"] == "unless-stopped"
        # Check standard ports (not 8080/8443)
        assert "80:80" in service["ports"]
        assert "443:443" in service["ports"]
        # Check macvlan network with static IP
        assert "syrvis-macvlan" in service["networks"]
        assert service["networks"]["syrvis-macvlan"]["ipv4_address"] == "192.168.0.100"
        assert len(service["volumes"]) == 5
        assert "traefik.enable=true" in service["labels"]

    def test_generate_portainer_service(self, temp_config_file):
        """Test Portainer service generation."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        service = generator._generate_portainer_service()

        assert service["image"] == "portainer/portainer-ce:2.19.4"
        assert service["container_name"] == "portainer"
        assert service["restart"] == "unless-stopped"
        assert "proxy" in service["networks"]
        assert len(service["volumes"]) == 2
        assert any("portainer" in label for label in service["labels"])

    def test_generate_cloudflared_service(self, temp_config_file):
        """Test Cloudflared service generation."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        service = generator._generate_cloudflared_service()

        assert service is not None
        assert service["image"] == "cloudflare/cloudflared:2024.1.5"
        assert service["container_name"] == "cloudflared"
        assert "TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}" in service["environment"]
        assert service["command"] == "tunnel --no-autoupdate run"

    def test_generate_cloudflared_service_not_in_config(self, temp_config_without_cloudflared):
        """Test Cloudflared service generation when not in config."""
        generator = ComposeGenerator(str(temp_config_without_cloudflared))
        generator.load_config()

        service = generator._generate_cloudflared_service()

        assert service is None

    def test_generate_compose_with_all_services(self, temp_config_file, network_env_vars):
        """Test complete compose generation with all services."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        compose = generator.generate_compose()

        assert compose["version"] == "3.8"
        assert "services" in compose
        assert "traefik" in compose["services"]
        assert "portainer" in compose["services"]
        assert "cloudflared" in compose["services"]
        assert "networks" in compose
        assert "syrvis-macvlan" in compose["networks"]
        assert "proxy" in compose["networks"]

    def test_generate_compose_without_cloudflared(
        self, temp_config_without_cloudflared, network_env_vars
    ):
        """Test compose generation without Cloudflared."""
        generator = ComposeGenerator(str(temp_config_without_cloudflared))
        generator.load_config()

        compose = generator.generate_compose()

        assert "traefik" in compose["services"]
        assert "portainer" in compose["services"]
        assert "cloudflared" not in compose["services"]

    def test_generate_compose_before_load_config(self):
        """Test generating compose before loading config raises error."""
        generator = ComposeGenerator("dummy.yaml")

        with pytest.raises(ValueError, match="Build config not loaded"):
            generator.generate_compose()

    def test_save_compose(self, temp_config_file, tmp_path, network_env_vars):
        """Test saving compose file."""
        output_file = tmp_path / "docker-compose.yaml"
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        generator.save_compose(str(output_file))

        assert output_file.exists()

        # Verify content
        with open(output_file, "r") as f:
            saved_compose = yaml.safe_load(f)

        assert saved_compose["version"] == "3.8"
        assert "traefik" in saved_compose["services"]
        assert "portainer" in saved_compose["services"]

    def test_save_compose_creates_directory(self, temp_config_file, tmp_path, network_env_vars):
        """Test saving compose file creates parent directories."""
        output_file = tmp_path / "subdir" / "docker-compose.yaml"
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        generator.save_compose(str(output_file))

        assert output_file.exists()
        assert output_file.parent.exists()

    def test_generate_and_save(self, temp_config_file, tmp_path, network_env_vars):
        """Test convenience method."""
        output_file = tmp_path / "docker-compose.yaml"
        generator = ComposeGenerator(str(temp_config_file))

        compose = generator.generate_and_save(output_path=str(output_file))

        assert output_file.exists()
        assert compose["version"] == "3.8"
        assert "traefik" in compose["services"]

    def test_generate_and_save_with_new_config_path(
        self, temp_config_file, tmp_path, network_env_vars
    ):
        """Test convenience method with config path override."""
        output_file = tmp_path / "docker-compose.yaml"
        generator = ComposeGenerator("dummy.yaml")

        compose = generator.generate_and_save(
            config_path=str(temp_config_file), output_path=str(output_file)
        )

        assert output_file.exists()
        assert compose["version"] == "3.8"


class TestHelperFunction:
    """Test module-level helper function."""

    def test_generate_compose_from_config(self, temp_config_file, tmp_path, network_env_vars):
        """Test helper function."""
        output_file = tmp_path / "docker-compose.yaml"

        compose = generate_compose_from_config(
            config_path=str(temp_config_file), output_path=str(output_file)
        )

        assert output_file.exists()
        assert compose["version"] == "3.8"
        assert "traefik" in compose["services"]
        assert "portainer" in compose["services"]

        # Verify file content
        with open(output_file, "r") as f:
            saved_compose = yaml.safe_load(f)

        assert saved_compose == compose


class TestDockerImageVersions:
    """Test that correct Docker image versions are used."""

    def test_traefik_version_from_config(self, temp_config_file, network_env_vars):
        """Test that Traefik uses version from config."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        traefik_image = compose["services"]["traefik"]["image"]
        assert traefik_image == "library/traefik:v3.0.0"

    def test_portainer_version_from_config(self, temp_config_file, network_env_vars):
        """Test that Portainer uses version from config."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        portainer_image = compose["services"]["portainer"]["image"]
        assert portainer_image == "portainer/portainer-ce:2.19.4"

    def test_cloudflared_version_from_config(self, temp_config_file, network_env_vars):
        """Test that Cloudflared uses version from config."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        cloudflared_image = compose["services"]["cloudflared"]["image"]
        assert cloudflared_image == "cloudflare/cloudflared:2024.1.5"


class TestNetworkValidation:
    """Test network configuration validation."""

    def test_missing_env_vars(self, temp_config_file, monkeypatch):
        """Test error when network env vars missing."""
        # Ensure env vars are not set
        monkeypatch.delenv("NETWORK_INTERFACE", raising=False)
        monkeypatch.delenv("NETWORK_SUBNET", raising=False)
        monkeypatch.delenv("NETWORK_GATEWAY", raising=False)
        monkeypatch.delenv("TRAEFIK_IP", raising=False)

        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        with pytest.raises(ValueError, match="Missing required network environment variables"):
            generator.generate_compose()

    def test_invalid_subnet(self, temp_config_file, monkeypatch):
        """Test error with invalid subnet."""
        monkeypatch.setenv("NETWORK_INTERFACE", "ovs_eth0")
        monkeypatch.setenv("NETWORK_SUBNET", "invalid-subnet")
        monkeypatch.setenv("NETWORK_GATEWAY", "192.168.0.1")
        monkeypatch.setenv("TRAEFIK_IP", "192.168.0.100")

        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        with pytest.raises(ValueError, match="Invalid subnet format"):
            generator.generate_compose()

    def test_gateway_not_in_subnet(self, temp_config_file, monkeypatch):
        """Test error when gateway not in subnet."""
        monkeypatch.setenv("NETWORK_INTERFACE", "ovs_eth0")
        monkeypatch.setenv("NETWORK_SUBNET", "192.168.0.0/24")
        monkeypatch.setenv("NETWORK_GATEWAY", "10.0.0.1")  # Different subnet
        monkeypatch.setenv("TRAEFIK_IP", "192.168.0.100")

        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        with pytest.raises(ValueError, match="Gateway .* not in subnet"):
            generator.generate_compose()

    def test_traefik_ip_not_in_subnet(self, temp_config_file, monkeypatch):
        """Test error when Traefik IP not in subnet."""
        monkeypatch.setenv("NETWORK_INTERFACE", "ovs_eth0")
        monkeypatch.setenv("NETWORK_SUBNET", "192.168.0.0/24")
        monkeypatch.setenv("NETWORK_GATEWAY", "192.168.0.1")
        monkeypatch.setenv("TRAEFIK_IP", "10.0.0.100")  # Different subnet

        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()

        with pytest.raises(ValueError, match="Traefik IP .* not in subnet"):
            generator.generate_compose()


class TestComposeStructure:
    """Test the structure of generated compose file."""

    def test_network_configuration(self, temp_config_file, network_env_vars):
        """Test network configuration includes macvlan."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        assert "networks" in compose
        assert "syrvis-macvlan" in compose["networks"]
        assert "proxy" in compose["networks"]

        # Check macvlan config
        macvlan = compose["networks"]["syrvis-macvlan"]
        assert macvlan["driver"] == "macvlan"
        assert macvlan["driver_opts"]["parent"] == "ovs_eth0"

        # Check bridge config
        assert compose["networks"]["proxy"]["name"] == "proxy"
        assert compose["networks"]["proxy"]["driver"] == "bridge"

    def test_traefik_uses_macvlan(self, temp_config_file, network_env_vars):
        """Test that Traefik uses macvlan network."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        traefik = compose["services"]["traefik"]
        assert "syrvis-macvlan" in traefik["networks"]
        assert traefik["networks"]["syrvis-macvlan"]["ipv4_address"] == "192.168.0.100"

    def test_other_services_use_bridge(self, temp_config_file, network_env_vars):
        """Test that Portainer and Cloudflared use bridge network."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        for service_name in ["portainer", "cloudflared"]:
            service = compose["services"][service_name]
            assert "networks" in service
            assert "proxy" in service["networks"]

    def test_security_options(self, temp_config_file, network_env_vars):
        """Test security options are set."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        # Check Traefik and Portainer have security options
        for service_name in ["traefik", "portainer"]:
            service = compose["services"][service_name]
            assert "security_opt" in service
            assert "no-new-privileges:true" in service["security_opt"]

    def test_restart_policies(self, temp_config_file, network_env_vars):
        """Test restart policies are set."""
        generator = ComposeGenerator(str(temp_config_file))
        generator.load_config()
        compose = generator.generate_compose()

        for service_name, service in compose["services"].items():
            assert service["restart"] == "unless-stopped"
