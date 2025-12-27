"""
Docker container management for SyrvisCore.

Manages core services using Docker SDK and docker-compose.
"""

import subprocess
from datetime import datetime
from typing import Dict, List, Optional

import docker
from docker.errors import DockerException
from dotenv import load_dotenv

from syrviscore.paths import (
    get_docker_compose_path,
    get_env_path,
    get_syrvis_home,
    validate_docker_compose_exists,
)
from syrviscore.traefik_config import (
    generate_traefik_dynamic_config,
    generate_traefik_static_config,
)


class DockerConnectionError(Exception):
    """Raised when cannot connect to Docker daemon."""

    pass


class DockerError(Exception):
    """Raised when Docker operations fail."""

    pass


class DockerManager:
    """Manage Docker containers for SyrvisCore core services."""

    # Project name for docker-compose
    PROJECT_NAME = "syrviscore"

    # Core service names
    CORE_SERVICES = ["traefik", "portainer", "cloudflared"]

    def __init__(self):
        """
        Initialize Docker manager.

        Raises:
            DockerConnectionError: If cannot connect to Docker daemon
        """
        try:
            self.client = docker.from_env()
            # Test connection
            self.client.ping()
        except DockerException as e:
            raise DockerConnectionError(
                f"Cannot connect to Docker daemon. Is Docker running?\nError: {e}"
            )

    def get_core_containers(self) -> List[docker.models.containers.Container]:
        """
        Get list of core service containers.

        Returns:
            List of Container objects for core services

        Raises:
            DockerConnectionError: If Docker daemon unreachable
        """
        try:
            # Find containers by project label
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={self.PROJECT_NAME}"},
            )
            return containers
        except DockerException as e:
            raise DockerConnectionError(f"Failed to list containers: {e}")

    def _run_compose_command(self, command: List[str]) -> subprocess.CompletedProcess:
        """
        Run docker-compose command.

        Args:
            command: Command arguments (e.g., ['up', '-d'])

        Returns:
            Completed process result

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If command fails
        """
        validate_docker_compose_exists()
        syrvis_home = get_syrvis_home()

        full_command = [
            "docker-compose",
            "-f",
            str(get_docker_compose_path()),
            "-p",
            self.PROJECT_NAME,
        ] + command

        result = subprocess.run(
            full_command, cwd=str(syrvis_home), capture_output=True, text=True, check=False
        )

        if result.returncode != 0:
            # Show actual docker-compose error output
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            raise DockerError(f"Failed to run docker-compose {' '.join(command)}:\n{error_msg}")

        return result

    def _create_traefik_files(self) -> None:
        """
        Create required Traefik files and directories with configuration.

        Creates/updates:
        - data/traefik/traefik.yml (mode 0644) - Static configuration (always updated)
        - data/traefik/config/dynamic.yml (mode 0644) - Dynamic configuration (always updated)
        - data/traefik/acme.json (mode 0600) - Let's Encrypt certificates (created if missing, never overwritten)

        This method is idempotent and safe to call multiple times.
        """
        # Load .env to get DOMAIN, ACME_EMAIL etc for traefik config
        load_dotenv(get_env_path(), override=True)

        syrvis_home = get_syrvis_home()
        traefik_data = syrvis_home / "data" / "traefik"

        # Ensure traefik data directory exists
        traefik_data.mkdir(parents=True, exist_ok=True)

        # Write traefik.yml static configuration (always update)
        traefik_yml = traefik_data / "traefik.yml"
        traefik_yml.write_text(generate_traefik_static_config())
        traefik_yml.chmod(0o644)

        # Create config DIRECTORY for dynamic configuration files
        config_dir = traefik_data / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Write dynamic configuration (always update)
        dynamic_yml = config_dir / "dynamic.yml"
        dynamic_yml.write_text(generate_traefik_dynamic_config())
        dynamic_yml.chmod(0o644)

        # Create acme.json ONLY if it doesn't exist (preserves certificates)
        acme_file = traefik_data / "acme.json"
        if not acme_file.exists():
            acme_file.touch()
            acme_file.chmod(0o600)

    def _ensure_macvlan_shim(self) -> None:
        """
        Ensure macvlan shim interface exists for host-to-container communication.

        This is required because macvlan containers cannot communicate with
        their host directly. The shim allows Traefik to reach NAS services.
        """
        import os
        from . import privileged_ops

        # Get network settings from environment
        interface = os.getenv("NETWORK_INTERFACE", "")
        traefik_ip = os.getenv("TRAEFIK_IP", "")
        shim_ip = os.getenv("SHIM_IP", "")

        if not interface or not traefik_ip:
            return  # Skip if not configured

        # If SHIM_IP not set, calculate from traefik_ip + 1 for backwards compatibility
        if not shim_ip:
            try:
                parts = traefik_ip.split('.')
                last_octet = int(parts[3])
                shim_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.{last_octet + 1}"
            except (IndexError, ValueError):
                return  # Skip if IP format is unexpected

        # Create shim (requires root, but we're already elevated for Docker)
        ok, msg = privileged_ops.ensure_macvlan_shim(interface, traefik_ip, shim_ip)
        if not ok:
            # Log warning but don't fail - services might still work
            import sys
            print(f"Warning: {msg}", file=sys.stderr)

    def start_core_services(self) -> None:
        """
        Start core services using docker-compose.

        Creates required Traefik files and macvlan shim before starting services.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        # Create required Traefik files
        self._create_traefik_files()

        # Ensure macvlan shim exists for host-to-container communication
        self._ensure_macvlan_shim()

        # Start services
        self._run_compose_command(["up", "-d"])

    def stop_core_services(self) -> None:
        """
        Stop core services using docker-compose.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        self._run_compose_command(["stop"])

    def clean_core_services(self, remove_volumes: bool = False) -> dict:
        """
        Remove all SyrvisCore containers and networks.

        This is useful for cleaning up before reinstall or when containers/networks
        are in a bad state.

        Args:
            remove_volumes: If True, also remove named volumes

        Returns:
            Dictionary with counts of removed resources

        Raises:
            DockerConnectionError: If Docker daemon unreachable
        """
        results = {
            "containers_removed": 0,
            "containers_stopped": [],  # List of stopped container names
            "networks_removed": 0,
            "networks_cleaned": [],  # List of removed network names
            "volumes_removed": 0,
            "volumes_cleaned": [],  # List of removed volume names
            "errors": [],
        }

        # Stop and remove containers by name (more reliable than compose labels)
        for container_name in self.CORE_SERVICES:
            try:
                container = self.client.containers.get(container_name)
                container.stop(timeout=10)
                container.remove(force=True)
                results["containers_removed"] += 1
                results["containers_stopped"].append(container_name)
            except docker.errors.NotFound:
                pass  # Container doesn't exist
            except Exception as e:
                results["errors"].append(f"Container {container_name}: {e}")

        # Also try to get containers by compose project label (catches renamed containers)
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={self.PROJECT_NAME}"},
            )
            for container in containers:
                try:
                    container_name = container.name
                    container.stop(timeout=10)
                    container.remove(force=True)
                    results["containers_removed"] += 1
                    if container_name not in results["containers_stopped"]:
                        results["containers_stopped"].append(container_name)
                except Exception as e:
                    results["errors"].append(f"Container {container.name}: {e}")
        except Exception as e:
            results["errors"].append(f"Listing containers: {e}")

        # Remove networks - try various naming patterns
        network_patterns = [
            "proxy",
            "syrvis-macvlan",
            f"{self.PROJECT_NAME}_syrvis-macvlan",
            f"{self.PROJECT_NAME}_proxy",
            "config_syrvis-macvlan",  # Old naming pattern
            "config_proxy",
        ]

        for network_name in network_patterns:
            try:
                network = self.client.networks.get(network_name)
                # Disconnect any remaining containers first
                try:
                    network.reload()
                    for container_id in network.attrs.get("Containers", {}).keys():
                        try:
                            network.disconnect(container_id, force=True)
                        except Exception:
                            pass
                except Exception:
                    pass
                network.remove()
                results["networks_removed"] += 1
                results["networks_cleaned"].append(network_name)
            except docker.errors.NotFound:
                pass  # Network doesn't exist
            except Exception as e:
                # Only log error if it's not a "not found" type error
                if "not found" not in str(e).lower():
                    results["errors"].append(f"Network {network_name}: {e}")

        # Optionally remove volumes
        if remove_volumes:
            volume_patterns = [
                f"{self.PROJECT_NAME}_traefik_data",
                f"{self.PROJECT_NAME}_portainer_data",
            ]
            for volume_name in volume_patterns:
                try:
                    volume = self.client.volumes.get(volume_name)
                    volume.remove(force=True)
                    results["volumes_removed"] += 1
                    results["volumes_cleaned"].append(volume_name)
                except docker.errors.NotFound:
                    pass
                except Exception as e:
                    results["errors"].append(f"Volume {volume_name}: {e}")

        return results

    def reset_core_services(self) -> dict:
        """
        Clean all containers/networks and start fresh.

        This is the nuclear option - removes everything and starts from scratch.
        Useful when reinstalling or when things are in a broken state.

        Returns:
            Dictionary with clean results and start status

        Raises:
            DockerConnectionError: If Docker daemon unreachable
            DockerError: If start fails after clean
        """
        # First clean everything
        clean_results = self.clean_core_services()

        # Now start fresh
        self._create_traefik_files()
        self._run_compose_command(["up", "-d"])

        clean_results["started"] = True
        return clean_results

    def restart_core_services(self) -> None:
        """
        Restart core services using docker-compose.

        Ensures Traefik configuration files are valid before restarting.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        # Ensure Traefik configs exist and are up-to-date before restarting
        self._create_traefik_files()

        # Restart services
        self._run_compose_command(["restart"])

    def get_container_status(self) -> Dict[str, Dict[str, str]]:
        """
        Get status of all core service containers.

        Returns:
            Dictionary mapping service name to status info:
            {
                "traefik": {
                    "name": "traefik",
                    "status": "running",
                    "uptime": "2 hours ago",
                    "image": "traefik:v3.0.0"
                },
                ...
            }

        Raises:
            DockerConnectionError: If Docker daemon unreachable
        """
        containers = self.get_core_containers()
        status_dict = {}

        for container in containers:
            # Get service name from compose label
            service_name = container.labels.get("com.docker.compose.service", container.name)

            # Calculate uptime
            created_at = container.attrs.get("Created", "")
            if created_at:
                try:
                    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    uptime_seconds = (datetime.now(created.tzinfo) - created).total_seconds()
                    uptime = self._format_uptime(uptime_seconds)
                except Exception:
                    uptime = "Unknown"
            else:
                uptime = "Unknown"

            status_dict[service_name] = {
                "name": container.name,
                "status": container.status,
                "uptime": uptime,
                "image": container.image.tags[0] if container.image.tags else "Unknown",
            }

        return status_dict

    def get_container_logs(
        self, service: Optional[str] = None, follow: bool = False, tail: int = 100
    ) -> str:
        """
        Get logs from container(s).

        Args:
            service: Service name to get logs for (None = all services)
            follow: Whether to follow log output
            tail: Number of lines to show from end

        Returns:
            Log output as string (if not following)
            For follow=True, streams logs to stdout

        Raises:
            DockerConnectionError: If Docker daemon unreachable
            ValueError: If service not found
        """
        containers = self.get_core_containers()

        if service:
            # Find specific service
            container = None
            for c in containers:
                service_name = c.labels.get("com.docker.compose.service")
                if service_name == service:
                    container = c
                    break

            if not container:
                available = [c.labels.get("com.docker.compose.service", c.name) for c in containers]
                raise ValueError(
                    f"Service '{service}' not found. Available services: {', '.join(available)}"
                )

            containers = [container]

        if not containers:
            return "No containers found"

        if follow:
            # For follow mode, use docker-compose logs
            try:
                cmd = ["logs", "-f", "--tail", str(tail)]
                if service:
                    cmd.append(service)

                self._run_compose_command(cmd)
            except subprocess.CalledProcessError:
                # User likely interrupted with Ctrl+C
                pass
            return ""
        else:
            # Get logs from all containers
            logs = []
            for container in containers:
                service_name = container.labels.get("com.docker.compose.service", container.name)
                logs.append(f"=== {service_name} ===")
                try:
                    container_logs = container.logs(tail=tail, timestamps=True).decode("utf-8")
                    logs.append(container_logs)
                except Exception as e:
                    logs.append(f"Error getting logs: {e}")
                logs.append("")

            return "\n".join(logs)

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """
        Format uptime in seconds to human-readable string.

        Args:
            seconds: Uptime in seconds

        Returns:
            Formatted string (e.g., "2 hours", "30 minutes")
        """
        if seconds < 60:
            return f"{int(seconds)} seconds"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''}"
