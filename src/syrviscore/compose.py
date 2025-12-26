"""
Docker Compose configuration generator for SyrvisCore.

This module reads build/config.yaml (for Docker images) and .env file
(for network settings) to generate docker-compose.yaml with Traefik, Portainer,
and Cloudflared services.
"""

import ipaddress
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class ComposeGenerator:
    """Generate docker-compose.yaml from build configuration and environment variables."""

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

    def _get_network_config_from_env(self) -> Dict[str, str]:
        """
        Read network configuration from environment variables.

        Returns:
            Dictionary with network configuration

        Raises:
            ValueError: If required environment variables are missing
        """
        required_vars = {
            "NETWORK_INTERFACE": "Network interface (e.g., ovs_eth0)",
            "NETWORK_SUBNET": "Network subnet in CIDR notation (e.g., 192.168.1.0/24)",
            "NETWORK_GATEWAY": "Network gateway IP (e.g., 192.168.1.1)",
            "TRAEFIK_IP": "Traefik dedicated IP address (e.g., 192.168.1.100)",
        }

        missing = []
        for var, description in required_vars.items():
            if not os.getenv(var):
                missing.append(f"  - {var}: {description}")

        if missing:
            error_msg = (
                "Missing required network environment variables:\n"
                + "\n".join(missing)
                + "\n\nPlease set these variables in your .env file."
            )
            raise ValueError(error_msg)

        return {
            "interface": os.getenv("NETWORK_INTERFACE"),
            "subnet": os.getenv("NETWORK_SUBNET"),
            "gateway": os.getenv("NETWORK_GATEWAY"),
            "traefik_ip": os.getenv("TRAEFIK_IP"),
        }

    def _validate_network_config(self, network_config: Dict[str, str]) -> None:
        """
        Validate network configuration.

        Args:
            network_config: Dictionary with network settings

        Raises:
            ValueError: If network config is invalid
        """
        # Validate subnet format
        try:
            subnet = ipaddress.ip_network(network_config["subnet"], strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid subnet format '{network_config['subnet']}': {e}")

        # Validate gateway is in subnet
        try:
            gateway = ipaddress.ip_address(network_config["gateway"])
            if gateway not in subnet:
                raise ValueError(
                    f"Gateway {gateway} not in subnet {subnet}. "
                    "Check your NETWORK_GATEWAY and NETWORK_SUBNET values."
                )
        except ValueError as e:
            if "not in subnet" in str(e):
                raise
            raise ValueError(f"Invalid gateway IP '{network_config['gateway']}': {e}")

        # Validate Traefik IP is in subnet
        try:
            traefik_ip = ipaddress.ip_address(network_config["traefik_ip"])
            if traefik_ip not in subnet:
                raise ValueError(
                    f"Traefik IP {traefik_ip} not in subnet {subnet}. "
                    "Check your TRAEFIK_IP and NETWORK_SUBNET values."
                )
        except ValueError as e:
            if "not in subnet" in str(e):
                raise
            raise ValueError(f"Invalid Traefik IP '{network_config['traefik_ip']}': {e}")

    def _generate_traefik_service(self, network_config: Dict[str, str]) -> Dict[str, Any]:
        """
        Generate Traefik service configuration with macvlan network.

        Traefik gets its own dedicated IP via macvlan network, allowing it
        to bind to standard ports 80/443 without conflicting with Synology nginx.

        Args:
            network_config: Network configuration from environment variables
        """
        image = self.build_config["docker_images"]["traefik"]["full_image"]
        traefik_ip = network_config["traefik_ip"]

        return {
            "image": image,
            "container_name": "traefik",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": {
                "syrvis-macvlan": {
                    "ipv4_address": traefik_ip,
                },
                "proxy": {},
            },
            # No port bindings needed - traefik has its own IP via macvlan
            "environment": ["TZ=UTC"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "../data/traefik/traefik.yml:/traefik.yml:ro",
                "../data/traefik/config/:/config/:ro",
                "../data/traefik/acme.json:/acme.json",
                "../data/traefik/logs:/logs",
            ],
            "labels": [
                "traefik.enable=false",
            ],
        }

    def _generate_portainer_service(self) -> Dict[str, Any]:
        """Generate Portainer service configuration on bridge network."""
        image = self.build_config["docker_images"]["portainer"]["full_image"]

        return {
            "image": image,
            "container_name": "portainer",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "../data/portainer:/data",
            ],
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.portainer.entrypoints=websecure",
                "traefik.http.routers.portainer.rule=Host(`portainer.${DOMAIN}`)",
                "traefik.http.routers.portainer.tls=true",
                "traefik.http.services.portainer.loadbalancer.server.port=9000",
            ],
        }

    def _generate_cloudflared_service(self) -> Optional[Dict[str, Any]]:
        """Generate Cloudflared service configuration on bridge network."""
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

    def _generate_networks(self, network_config: Dict[str, str]) -> Dict[str, Any]:
        """
        Generate network configurations.

        Creates two networks:
        - syrvis-macvlan: Macvlan network for Traefik with dedicated IP
        - proxy: Bridge network for other services

        Args:
            network_config: Network configuration from environment variables
        """
        return {
            "syrvis-macvlan": {
                "driver": "macvlan",
                "driver_opts": {
                    "parent": network_config["interface"],
                },
                "ipam": {
                    "config": [
                        {
                            "subnet": network_config["subnet"],
                            "gateway": network_config["gateway"],
                        }
                    ]
                },
            },
            "proxy": {
                "name": "proxy",
                "driver": "bridge",
            },
        }

    def generate_compose(self) -> Dict[str, Any]:
        """
        Generate complete docker-compose configuration.

        Returns:
            Docker Compose configuration dictionary

        Raises:
            ValueError: If build config not loaded or network config invalid
        """
        if not self.build_config:
            raise ValueError("Build config not loaded. Call load_config() first.")

        # Get and validate network configuration from environment
        network_config = self._get_network_config_from_env()
        self._validate_network_config(network_config)

        compose = {
            "version": "3.8",
            "services": {
                "traefik": self._generate_traefik_service(network_config),
                "portainer": self._generate_portainer_service(),
            },
            "networks": self._generate_networks(network_config),
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
