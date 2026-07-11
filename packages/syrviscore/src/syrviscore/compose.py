"""
Docker Compose configuration generator for SyrvisCore.

This module reads build/config.yaml (for Docker images) and .env file
(for network settings) to generate docker-compose.yaml with the core-tier
services: Traefik, Portainer, Cloudflared, the SyrvisCore dashboard, and
(optionally) Cloudflare DDNS.
"""

import ipaddress
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# Default Docker image versions - used when config.yaml doesn't exist
DEFAULT_DOCKER_IMAGES = {
    "traefik": {
        "image": "traefik",
        "tag": "v3.6.5",
        "full_image": "traefik:v3.6.5",
        "description": "",
    },
    "portainer": {
        "image": "portainer/portainer-ce",
        "tag": "2.33.6-alpine",
        "full_image": "portainer/portainer-ce:2.33.6-alpine",
        "description": "",
    },
    "cloudflared": {
        "image": "cloudflare/cloudflared",
        "tag": "2025.11.1",
        "full_image": "cloudflare/cloudflared:2025.11.1",
        "description": "",
    },
    "dashboard": {
        "image": "ghcr.io/kevinteg/syrviscore-dashboard",
        "tag": "0.1.1",
        "full_image": "ghcr.io/kevinteg/syrviscore-dashboard:0.1.1",
        "description": "SyrvisCore web dashboard",
    },
    "cloudflare_ddns": {
        "image": "favonia/cloudflare-ddns",
        "tag": "1.15.1",
        "full_image": "favonia/cloudflare-ddns:1.15.1",
        "description": "Cloudflare Dynamic DNS updater",
    },
}


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
        Load build configuration from YAML file, or use defaults.

        If config.yaml doesn't exist, uses built-in default Docker image versions.

        Returns:
            Parsed configuration dictionary
        """
        if self.config_path.exists():
            with open(self.config_path, "r") as f:
                self.build_config = yaml.safe_load(f)

            if not self.build_config or "docker_images" not in self.build_config:
                raise ValueError("Invalid config: missing docker_images section")
        else:
            # Use built-in defaults
            self.build_config = {
                "metadata": {
                    "description": "Using default Docker image versions",
                },
                "docker_images": DEFAULT_DOCKER_IMAGES,
            }

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

        service = {
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
                # HTTP router (redirect to HTTPS)
                "traefik.http.routers.portainer-http.entrypoints=web",
                "traefik.http.routers.portainer-http.rule=Host(`portainer.${DOMAIN}`)",
                "traefik.http.routers.portainer-http.middlewares=https-redirect@file",
                # HTTPS router (with Let's Encrypt)
                "traefik.http.routers.portainer.entrypoints=websecure",
                "traefik.http.routers.portainer.rule=Host(`portainer.${DOMAIN}`)",
                "traefik.http.routers.portainer.tls=true",
                "traefik.http.routers.portainer.tls.certresolver=letsencrypt",
                # Service
                "traefik.http.services.portainer.loadbalancer.server.port=9000",
            ],
        }

        # Add admin password file if it exists
        # This sets the initial admin password on first run
        # Portainer ignores this flag if admin user already exists
        password_file = Path(os.environ.get("SYRVIS_HOME", "")) / "config" / ".portainer-password"
        if password_file.exists():
            service["command"] = "--admin-password-file /run/secrets/portainer-password"
            service["volumes"].append(
                "../config/.portainer-password:/run/secrets/portainer-password:ro"
            )

        return service

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
            "environment": [
                "TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}",
                # Expose the metrics/`/ready` server on the proxy network so the
                # dashboard can report real tunnel connectivity (not just container up).
                "TUNNEL_METRICS=0.0.0.0:20241",
            ],
            "command": "tunnel --no-autoupdate run",
        }

    def _generate_dashboard_service(self) -> Optional[Dict[str, Any]]:
        """Generate the SyrvisCore dashboard service (web observability + management).

        Emitted whenever a ``dashboard`` image is configured. Runs on the ``proxy``
        network so it can reach traefik:8080 / portainer:9000 / cloudflared:20241,
        holds the docker socket for container-safe management, and mounts the
        config/data/manifest so the in-process ``syrviscore`` library resolves
        ``SYRVIS_HOME``.
        """
        if "dashboard" not in self.build_config["docker_images"]:
            return None

        image = self.build_config["docker_images"]["dashboard"]["full_image"]
        stack = getattr(self, "_stack", None)
        subdomain = stack.setting("dashboard", "subdomain") if stack is not None else None
        if not subdomain:
            subdomain = os.getenv("DASHBOARD_SUBDOMAIN", "dash")

        return {
            "image": image,
            "container_name": "syrviscore-dashboard",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "environment": [
                "SYRVIS_HOME=/syrvis",
                "DASHBOARD_AUTH_MODE=${DASHBOARD_AUTH_MODE:-none}",
                "DASHBOARD_SESSION_SECRET=${DASHBOARD_SESSION_SECRET:-}",
                "ENABLE_L2_MUTATIONS=${ENABLE_L2_MUTATIONS:-false}",
                "SSH_TARGET=${SSH_TARGET:-nas}",
                "CLOUDFLARE_ACCESS_TEAM=${CLOUDFLARE_ACCESS_TEAM:-}",
                "CLOUDFLARE_ACCESS_AUD=${CLOUDFLARE_ACCESS_AUD:-}",
                "OIDC_ISSUER=${OIDC_ISSUER:-}",
                "OIDC_CLIENT_ID=${OIDC_CLIENT_ID:-}",
                "OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET:-}",
                "OIDC_REDIRECT_URL=${OIDC_REDIRECT_URL:-}",
            ],
            "volumes": [
                # rw: container control (start/stop/restart) is a write op on the socket.
                "/var/run/docker.sock:/var/run/docker.sock",
                "../config:/syrvis/config:ro",
                "../data:/syrvis/data",
                # so paths.get_syrvis_home() trusts SYRVIS_HOME (it looks for the manifest).
                "../.syrviscore-manifest.json:/syrvis/.syrviscore-manifest.json:ro",
            ],
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.dashboard-http.entrypoints=web",
                "traefik.http.routers.dashboard-http.rule=Host(`" + subdomain + ".${DOMAIN}`)",
                "traefik.http.routers.dashboard-http.middlewares=https-redirect@file",
                "traefik.http.routers.dashboard.entrypoints=websecure",
                "traefik.http.routers.dashboard.rule=Host(`" + subdomain + ".${DOMAIN}`)",
                "traefik.http.routers.dashboard.tls=true",
                "traefik.http.routers.dashboard.tls.certresolver=letsencrypt",
                "traefik.http.services.dashboard.loadbalancer.server.port=8000",
            ],
        }

    def _generate_ddns_service(self) -> Optional[Dict[str, Any]]:
        """Generate the Cloudflare DDNS service (favonia/cloudflare-ddns).

        Optional like cloudflared: only emitted when a ``CLOUDFLARE_API_TOKEN`` is
        configured (else the dashboard's DDNS probe reports ``not_configured``).
        """
        if "cloudflare_ddns" not in self.build_config["docker_images"]:
            return None
        if not os.getenv("CLOUDFLARE_API_TOKEN"):
            return None

        image = self.build_config["docker_images"]["cloudflare_ddns"]["full_image"]
        return {
            "image": image,
            "container_name": "cloudflare-ddns",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "environment": [
                "CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN}",
                "DOMAINS=${CLOUDFLARE_DDNS_RECORDS}",
                "PROXIED=${CLOUDFLARE_DDNS_PROXIED:-true}",
            ],
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

    def generate_compose(self, stack=None) -> Dict[str, Any]:
        """
        Generate complete docker-compose configuration.

        Args:
            stack: an explicit ``stack.Stack`` declaring which optional core
                services to emit. When None, it is loaded from
                ``config/stack.yaml`` (falling back to an env-inferred default).

        Returns:
            Docker Compose configuration dictionary

        Raises:
            ValueError: If build config not loaded or network config invalid
        """
        if not self.build_config:
            raise ValueError("Build config not loaded. Call load_config() first.")

        # Which core-tier services this instance declares (config/stack.yaml).
        from . import stack as stack_mod

        self._stack = stack if stack is not None else stack_mod.load_stack()

        # Get and validate network configuration from environment
        network_config = self._get_network_config_from_env()
        self._validate_network_config(network_config)

        compose = {
            "version": "3.8",
            "services": {
                # Primordial: always present.
                "traefik": self._generate_traefik_service(network_config),
                "portainer": self._generate_portainer_service(),
            },
            "networks": self._generate_networks(network_config),
        }

        # Optional core services — emitted only when declared enabled in the stack
        # (and, for cloudflared/DDNS, when their config is present).
        if self._stack.is_enabled("cloudflared"):
            cloudflared = self._generate_cloudflared_service()
            if cloudflared:
                compose["services"]["cloudflared"] = cloudflared

        if self._stack.is_enabled("dashboard"):
            dashboard = self._generate_dashboard_service()
            if dashboard:
                compose["services"]["syrviscore-dashboard"] = dashboard

        if self._stack.is_enabled("cloudflare_ddns"):
            ddns = self._generate_ddns_service()
            if ddns:
                compose["services"]["cloudflare-ddns"] = ddns

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
