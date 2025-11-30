"""
Docker Compose configuration generator for SyrvisCore.

This module reads build/config.yaml and generates a docker-compose.yaml
with Traefik, Portainer, and Cloudflared services.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class ComposeGenerator:
    """Generate docker-compose.yaml from build configuration."""

    def __init__(self, config_path: str = "build/config.yaml"):
        """
        Initialize the compose generator.

        Args:
            config_path: Path to build configuration file
        """
        self.config_path = Path(config_path)
        self.build_config: Optional[Dict[str, Any]] = None

    def load_config(self) -> Dict[str, Any]:
        """
        Load build configuration from YAML file.

        Returns:
            Parsed configuration dictionary

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If config file is invalid YAML
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path, "r") as f:
            self.build_config = yaml.safe_load(f)

        if not self.build_config or "docker_images" not in self.build_config:
            raise ValueError("Invalid config: missing docker_images section")

        return self.build_config

    def _generate_traefik_service(self) -> Dict[str, Any]:
        """Generate Traefik service configuration."""
        image = self.build_config["docker_images"]["traefik"]["full_image"]

        return {
            "image": image,
            "container_name": "traefik",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "ports": ["80:80", "443:443"],
            "environment": ["TZ=UTC"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "./data/traefik/traefik.yml:/traefik.yml:ro",
                "./data/traefik/config/:/config/:ro",
                "./data/traefik/acme.json:/acme.json",
                "./data/traefik/logs:/logs",
            ],
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.traefik.entrypoints=https",
                "traefik.http.routers.traefik.rule=Host(`traefik.${DOMAIN}`)",
                "traefik.http.routers.traefik.service=api@internal",
            ],
        }

    def _generate_portainer_service(self) -> Dict[str, Any]:
        """Generate Portainer service configuration."""
        image = self.build_config["docker_images"]["portainer"]["full_image"]

        return {
            "image": image,
            "container_name": "portainer",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "./data/portainer:/data",
            ],
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.portainer.entrypoints=https",
                "traefik.http.routers.portainer.rule=Host(`portainer.${DOMAIN}`)",
                "traefik.http.services.portainer.loadbalancer.server.port=9000",
            ],
        }

    def _generate_cloudflared_service(self) -> Optional[Dict[str, Any]]:
        """Generate Cloudflared service configuration."""
        if "cloudflared" not in self.build_config["docker_images"]:
            return None

        image = self.build_config["docker_images"]["cloudflared"]["full_image"]

        return {
            "image": image,
            "container_name": "cloudflared",
            "restart": "unless-stopped",
            "networks": ["proxy"],
            "environment": ["TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}"],
            "command": "tunnel --no-autoupdate run",
        }

    def generate_compose(self) -> Dict[str, Any]:
        """
        Generate complete docker-compose configuration.

        Returns:
            Docker Compose configuration dictionary

        Raises:
            ValueError: If build config not loaded
        """
        if not self.build_config:
            raise ValueError("Build config not loaded. Call load_config() first.")

        compose = {
            "version": "3.8",
            "services": {
                "traefik": self._generate_traefik_service(),
                "portainer": self._generate_portainer_service(),
            },
            "networks": {
                "proxy": {
                    "name": "proxy",
                    "driver": "bridge",
                }
            },
        }

        # Add Cloudflared if configured
        cloudflared = self._generate_cloudflared_service()
        if cloudflared:
            compose["services"]["cloudflared"] = cloudflared

        return compose

    def save_compose(self, output_path: str = "docker-compose.yaml") -> None:
        """
        Save generated compose configuration to file.

        Args:
            output_path: Path where to save the compose file

        Raises:
            ValueError: If compose config not generated
        """
        compose = self.generate_compose()

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w") as f:
            yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    def generate_and_save(
        self, config_path: Optional[str] = None, output_path: str = "docker-compose.yaml"
    ) -> Dict[str, Any]:
        """
        Convenience method to load config, generate, and save compose file.

        Args:
            config_path: Path to build config (uses self.config_path if None)
            output_path: Path where to save the compose file

        Returns:
            Generated compose configuration
        """
        if config_path:
            self.config_path = Path(config_path)

        self.load_config()
        self.save_compose(output_path)
        return self.generate_compose()


def generate_compose_from_config(
    config_path: str = "build/config.yaml", output_path: str = "docker-compose.yaml"
) -> Dict[str, Any]:
    """
    Helper function to generate docker-compose.yaml from build config.

    Args:
        config_path: Path to build configuration file
        output_path: Path where to save the compose file

    Returns:
        Generated compose configuration dictionary
    """
    generator = ComposeGenerator(config_path)
    return generator.generate_and_save(output_path=output_path)
