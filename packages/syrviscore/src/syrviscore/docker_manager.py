"""
Docker container management for SyrvisCore.

Manages core services using Docker SDK and docker-compose.
"""

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import docker
from docker.errors import DockerException
from dotenv import load_dotenv

from syrviscore.compose_cmd import resolve_compose_cmd
from syrviscore.errors import SyrvisError
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


class DockerConnectionError(SyrvisError):
    """Raised when cannot connect to Docker daemon."""

    code = "docker_unreachable"


class DockerError(SyrvisError):
    """Raised when Docker operations fail."""

    code = "docker_error"


def write_traefik_config_files(syrvis_home: Optional[Path] = None) -> bool:
    """Write Traefik's static + dynamic config; return whether STATIC config changed.

    This is the single writer of ``traefik.yml`` (static) and ``config/dynamic.yml``
    (dynamic), plus a one-time ``acme.json`` create. Centralizing it lets every
    caller enforce the invariant that a STATIC-config change must be followed by a
    Traefik *restart* — Traefik only parses ``traefik.yml`` at process start, while
    the file-provider hot-reloads the dynamic ``/config`` dir. Regenerating the
    static file without restarting is why a change like ``ping: {}`` never took
    effect (the dashboard then reports "up (API reachable) but /ping returned 404").

    Idempotent. ``acme.json`` is created only if missing (never overwritten, to
    preserve issued certificates).

    Returns:
        True if the static ``traefik.yml`` content differed from what was on disk
        (so the caller should restart Traefik if it is running).
    """
    load_dotenv(get_env_path(), override=True)

    home = Path(syrvis_home) if syrvis_home is not None else get_syrvis_home()
    traefik_data = home / "data" / "traefik"
    traefik_data.mkdir(parents=True, exist_ok=True)

    static_path = traefik_data / "traefik.yml"
    new_static = generate_traefik_static_config()
    old_static = static_path.read_text() if static_path.exists() else None
    static_changed = old_static != new_static
    # Only touch the file on a REAL change: the stale-static drift check compares
    # the file's mtime against Traefik's StartedAt, so a no-op regeneration that
    # rewrote identical bytes would bump the mtime and raise a false
    # stale_static_config flag (observed live: `stack apply` with unchanged
    # content flipped the dashboard to degraded).
    if static_changed:
        static_path.write_text(new_static)
    static_path.chmod(0o644)

    config_dir = traefik_data / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    dynamic_path = config_dir / "dynamic.yml"
    dynamic_path.write_text(generate_traefik_dynamic_config())
    dynamic_path.chmod(0o644)

    acme_file = traefik_data / "acme.json"
    if not acme_file.exists():
        acme_file.touch()
        acme_file.chmod(0o600)

    return static_changed


def remove_disabled_core_containers() -> List[str]:
    """Stop and remove containers of OPTIONAL core services disabled in the stack.

    ``docker compose up -d`` never removes a service that vanished from the
    compose file, so ``stack disable <svc> --apply`` + ``syrvis start`` would
    otherwise leave the disabled container running forever — breaking the
    declarative promise that stack.yaml is intent the apply reconciles.

    Deliberately narrower than ``--remove-orphans``: only the known OPTIONAL
    core services (never primordial ones, never L2 or unrelated containers) and
    only exact container-name matches. Best-effort: returns the names actually
    removed and never raises.
    """
    from . import stack as stack_mod

    removed: List[str] = []
    try:
        st = stack_mod.load_stack()
    except Exception:  # noqa: BLE001 - unreadable stack: do nothing rather than guess
        return removed

    try:
        import docker

        client = docker.from_env()
    except Exception:  # noqa: BLE001 - docker unreachable: nothing to reconcile
        return removed

    for name in stack_mod.OPTIONAL:
        if st.is_enabled(name):
            continue
        container_name = stack_mod.CONTAINER_NAME[name]
        try:
            container = client.containers.get(container_name)
        except Exception:  # noqa: BLE001 - NotFound or daemon hiccup: skip
            continue
        try:
            container.stop(timeout=10)
            container.remove()
            removed.append(container_name)
        except Exception:  # noqa: BLE001 - best-effort; report only what succeeded
            pass

    return removed


def restart_traefik_if_running(timeout: int = 10) -> bool:
    """Best-effort restart of the running Traefik container.

    Traefik parses its STATIC config (``traefik.yml``) only at process start, so a
    regenerated static file is invisible to the live process until it restarts.
    Callers invoke this after :func:`write_traefik_config_files` reports a static
    change. No-op (and never raises) if Traefik is absent or Docker is unreachable
    — the manual fallback is ``docker restart traefik``.

    Returns:
        True if a restart was actually issued.
    """
    try:
        import docker

        container = docker.from_env().containers.get("traefik")
        if container.status == "running":
            container.restart(timeout=timeout)
            return True
    except Exception:  # noqa: BLE001 - best-effort; never fail the caller
        pass
    return False


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

        full_command = (
            resolve_compose_cmd()
            + [
                "-f",
                str(get_docker_compose_path()),
                "-p",
                self.PROJECT_NAME,
            ]
            + command
        )

        result = subprocess.run(
            full_command, cwd=str(syrvis_home), capture_output=True, text=True, check=False
        )

        if result.returncode != 0:
            # Show actual docker-compose error output
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            raise DockerError(f"Failed to run docker-compose {' '.join(command)}:\n{error_msg}")

        return result

    def _create_traefik_files(self) -> bool:
        """
        Create/refresh required Traefik files and directories.

        Delegates to :func:`write_traefik_config_files` (the single writer), which
        creates/updates:
        - data/traefik/traefik.yml (mode 0644) - Static configuration (always updated)
        - data/traefik/config/dynamic.yml (mode 0644) - Dynamic configuration (always updated)
        - data/traefik/acme.json (mode 0600) - Let's Encrypt certs (created if missing, never overwritten)

        Idempotent. Returns True if the STATIC config content changed (the caller
        must then restart Traefik for it to take effect).
        """
        return write_traefik_config_files()

    def _ensure_macvlan_shim(self) -> Optional[str]:
        """
        Ensure macvlan shim interface exists for host-to-container communication.

        This is required because macvlan containers cannot communicate with
        their host directly. The shim allows Traefik to reach NAS services.

        Returns:
            A warning string if the shim could not be created (services may still
            work), else None. The library never prints — the caller renders it,
            so the shared library stays silent for the dashboard/MCP adapters.
        """
        import os
        from . import privileged_ops

        # Get network settings from environment
        interface = os.getenv("NETWORK_INTERFACE", "")
        traefik_ip = os.getenv("TRAEFIK_IP", "")
        shim_ip = os.getenv("SHIM_IP", "")

        if not interface or not traefik_ip:
            return None  # Skip if not configured

        # If SHIM_IP not set, calculate from traefik_ip + 1 for backwards compatibility
        if not shim_ip:
            try:
                parts = traefik_ip.split(".")
                last_octet = int(parts[3])
                shim_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.{last_octet + 1}"
            except (IndexError, ValueError):
                return None  # Skip if IP format is unexpected

        # Create shim (requires root, but we're already elevated for Docker)
        ok, msg = privileged_ops.ensure_macvlan_shim(interface, traefik_ip, shim_ip)
        if not ok:
            # Surface as a warning but don't fail — services might still work.
            return str(msg)
        return None

    def start_core_services(self) -> List[str]:
        """
        Start core services using docker-compose.

        Creates required Traefik files and macvlan shim before starting services.

        Returns:
            A list of non-fatal warning strings (e.g. the macvlan shim could not be
            created). Callers may render them; existing callers safely ignore the
            return. The library itself never prints.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        warnings: List[str] = []

        # Create required Traefik files (note whether the STATIC config changed)
        static_changed = self._create_traefik_files()

        # Ensure macvlan shim exists for host-to-container communication
        shim_warning = self._ensure_macvlan_shim()
        if shim_warning:
            warnings.append(shim_warning)

        # Start services
        self._run_compose_command(["up", "-d"])

        # `docker compose up -d` recreates a container only when its compose
        # definition changes — a bind-mounted STATIC config edit (e.g. adding
        # `ping: {}` to traefik.yml) is invisible to it. If the static config
        # changed, restart Traefik so the running process re-reads traefik.yml.
        if static_changed:
            restart_traefik_if_running()

        return warnings

    def stop_core_services(self) -> None:
        """
        Stop core services using docker-compose.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        self._run_compose_command(["stop"])

    def pull_core_images(self) -> None:
        """
        Pull the images declared in docker-compose.yaml.

        Used by drift remediation so an image mismatch is corrected by
        fetching the declared version before recreating the container.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        self._run_compose_command(["pull"])

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
        Restart core services by force-recreating them from compose.

        Uses ``up -d --force-recreate`` rather than ``compose restart``: a plain
        restart re-reads static config but silently ignores compose-spec changes
        (new image tag, env, mounts), while ``up -d`` alone ignores bind-mounted
        config edits. Force-recreate converges BOTH, so `syrvis restart` is the
        one command that reliably applies whatever changed.

        Ensures Traefik configuration files are up-to-date before recreating.

        Raises:
            FileNotFoundError: If docker-compose.yaml missing
            DockerError: If docker-compose fails
        """
        # Ensure Traefik configs exist and are up-to-date before recreating
        self._create_traefik_files()

        # Recreate services (applies static config AND compose-spec changes)
        self._run_compose_command(["up", "-d", "--force-recreate"])

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

            # Use the image reference the container was created with (Config.Image),
            # not image.tags[0] — a pulled image can carry several tags and tags[0]
            # may not be the one compose declared (causes false image_mismatch drift).
            configured_image = container.attrs.get("Config", {}).get("Image")
            if not configured_image:
                configured_image = container.image.tags[0] if container.image.tags else "Unknown"

            status_dict[service_name] = {
                "name": container.name,
                "status": container.status,
                "uptime": uptime,
                "image": configured_image,
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
