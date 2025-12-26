"""
Docker container management for SyrvisCore.

Manages core services using Docker SDK and docker-compose.
"""

import subprocess
from datetime import datetime
from typing import Dict, List, Optional

import docker
from docker.errors import DockerException

from syrviscore.paths import (
    get_docker_compose_path,
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

    def start_core_services(self) -> None:
        """
        Start core services using docker-compose.

        Creates required Traefik files before starting services.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        # Create required Traefik files
        self._create_traefik_files()

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
