"""
Service manager for Layer 2 services.

Handles adding, removing, listing, and updating user-installed services.
Each service is defined by a syrvis-service.yaml file in a git repository.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .service_schema import ServiceDefinition, load_service_definition
from .traefik_config import ServiceTraefikConfig, get_domain_from_env


class ServiceManager:
    """Manage Layer 2 services for SyrvisCore."""

    def __init__(self, syrvis_home: Optional[Path] = None):
        """Initialize the service manager.

        Args:
            syrvis_home: Path to SYRVIS_HOME. Defaults to $SYRVIS_HOME env var.
        """
        if syrvis_home:
            self.syrvis_home = syrvis_home
        else:
            home = os.environ.get("SYRVIS_HOME", "")
            if not home:
                raise ValueError("SYRVIS_HOME environment variable not set")
            self.syrvis_home = Path(home)

        self.services_dir = self.syrvis_home / "services"
        self.compose_dir = self.syrvis_home / "compose"
        self.data_dir = self.syrvis_home / "data"
        self.traefik_config = ServiceTraefikConfig()

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        self.services_dir.mkdir(parents=True, exist_ok=True)
        self.compose_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _is_git_url(self, source: str) -> bool:
        """Check if source is a git URL."""
        return (
            source.startswith("https://")
            or source.startswith("http://")
            or source.startswith("file://")
            or source.startswith("git@")
            or source.startswith("git://")
            or source.endswith(".git")
        )

    def _clone_service(self, git_url: str) -> Tuple[bool, str, Optional[Path]]:
        """Clone a service from git.

        Args:
            git_url: Git repository URL

        Returns:
            Tuple of (success, message, service_path)
        """
        # Create temp directory for cloning
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "repo"

            try:
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", git_url, str(temp_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    return False, f"Failed to clone: {result.stderr}", None
            except subprocess.TimeoutExpired:
                return False, "Git clone timed out", None
            except FileNotFoundError:
                return False, "Git is not installed", None

            # Check for service definition
            yaml_path = temp_path / "syrvis-service.yaml"
            if not yaml_path.exists():
                return False, "No syrvis-service.yaml found in repository", None

            # Load and validate
            try:
                service = load_service_definition(yaml_path)
            except (ValueError, FileNotFoundError) as e:
                return False, f"Invalid service definition: {e}", None

            # Check if already installed
            target_dir = self.services_dir / service.name
            if target_dir.exists():
                return False, f"Service '{service.name}' is already installed", None

            # Move to services directory
            self._ensure_directories()
            shutil.move(str(temp_path), str(target_dir))

            return True, f"Cloned service '{service.name}'", target_dir

    def add(self, source: str, start: bool = True) -> Tuple[bool, str]:
        """Add a service from a git URL or registry name.

        Args:
            source: Git URL or registry service name
            start: Whether to start the service after adding

        Returns:
            Tuple of (success, message)
        """
        self._ensure_directories()

        # Clone from git
        if self._is_git_url(source):
            success, msg, service_path = self._clone_service(source)
            if not success:
                return False, msg
        else:
            # Future: registry lookup
            return False, f"Registry lookup not yet implemented. Use a git URL instead."

        # Load service definition
        try:
            service = load_service_definition(service_path)
            service.source_url = source if self._is_git_url(source) else None
        except Exception as e:
            # Cleanup on failure
            if service_path and service_path.exists():
                shutil.rmtree(service_path)
            return False, f"Failed to load service: {e}"

        # Create data directories
        service_data_dir = self.data_dir / service.name
        service_data_dir.mkdir(parents=True, exist_ok=True)

        # Copy config templates
        if service.config_templates:
            for template in service.config_templates:
                src = service_path / template.source
                dest = service_data_dir / template.dest
                if src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

        # Generate compose file
        compose_path = self._generate_compose_file(service)

        # Generate Traefik config
        try:
            domain = get_domain_from_env()
            traefik_path = self.traefik_config.write_config(service, domain)
        except ValueError as e:
            return False, f"Failed to configure Traefik: {e}"

        # Start service if requested
        if start:
            success, msg = self._start_service(service.name, compose_path)
            if not success:
                return False, f"Service installed but failed to start: {msg}"
            return True, f"Service '{service.name}' added and started"

        return True, f"Service '{service.name}' added (not started)"

    def _generate_compose_file(self, service: ServiceDefinition) -> Path:
        """Generate docker-compose file for a service.

        Args:
            service: Service definition

        Returns:
            Path to generated compose file
        """
        compose = {
            "version": "3.8",
            "services": {
                service.name: {
                    "image": service.image,
                    "container_name": service.container_name,
                    "restart": service.restart,
                    "networks": service.networks,
                }
            },
            "networks": {
                "proxy": {
                    "external": True,
                }
            },
        }

        svc = compose["services"][service.name]

        # Add environment if specified
        if service.environment:
            svc["environment"] = service.environment

        # Process volumes - convert relative paths to absolute
        if service.volumes:
            processed_volumes = []
            for vol in service.volumes:
                parts = vol.split(":")
                if len(parts) >= 2:
                    host_path = parts[0]
                    container_path = parts[1]
                    mode = parts[2] if len(parts) > 2 else "rw"

                    # If host path is relative, make it absolute to data dir
                    if not host_path.startswith("/") and not host_path.startswith("$"):
                        host_path = str(self.data_dir / service.name / host_path)

                    processed_volumes.append(f"{host_path}:{container_path}:{mode}")
                else:
                    # Named volume or other format - pass through
                    processed_volumes.append(vol)

            svc["volumes"] = processed_volumes

        # Add depends_on
        if service.depends_on:
            svc["depends_on"] = service.depends_on

        # Write compose file
        compose_path = self.compose_dir / f"{service.name}.yaml"
        with open(compose_path, "w") as f:
            yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

        return compose_path

    def _start_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        """Start a service using docker compose.

        Args:
            name: Service name
            compose_path: Path to compose file

        Returns:
            Tuple of (success, message)
        """
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "up", "-d"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                return False, result.stderr
            return True, "Started"
        except subprocess.TimeoutExpired:
            return False, "Docker compose timed out"
        except FileNotFoundError:
            return False, "Docker is not installed"

    def _stop_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        """Stop a service using docker compose.

        Args:
            name: Service name
            compose_path: Path to compose file

        Returns:
            Tuple of (success, message)
        """
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "down"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return False, result.stderr
            return True, "Stopped"
        except subprocess.TimeoutExpired:
            return False, "Docker compose timed out"
        except FileNotFoundError:
            return False, "Docker is not installed"

    def remove(self, name: str, purge: bool = False) -> Tuple[bool, str]:
        """Remove an installed service.

        Args:
            name: Service name
            purge: If True, also remove data directory

        Returns:
            Tuple of (success, message)
        """
        service_dir = self.services_dir / name
        compose_path = self.compose_dir / f"{name}.yaml"

        if not service_dir.exists() and not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        # Stop the service
        if compose_path.exists():
            self._stop_service(name, compose_path)
            compose_path.unlink()

        # Remove Traefik config
        self.traefik_config.remove_config(name)

        # Remove service definition
        if service_dir.exists():
            shutil.rmtree(service_dir)

        # Remove data if purging
        if purge:
            data_dir = self.data_dir / name
            if data_dir.exists():
                shutil.rmtree(data_dir)
            return True, f"Service '{name}' removed (data purged)"

        return True, f"Service '{name}' removed (data preserved)"

    def list(self) -> List[Dict[str, Any]]:
        """List all installed services.

        Returns:
            List of service info dictionaries
        """
        services = []

        if not self.services_dir.exists():
            return services

        for service_dir in self.services_dir.iterdir():
            if not service_dir.is_dir():
                continue

            yaml_path = service_dir / "syrvis-service.yaml"
            if not yaml_path.exists():
                continue

            try:
                service = load_service_definition(yaml_path)
                status = self._get_service_status(service.name)
                url = ""
                if service.traefik.enabled and service.traefik.subdomain:
                    try:
                        domain = get_domain_from_env()
                        url = f"https://{service.traefik.subdomain}.{domain}"
                    except ValueError:
                        pass

                services.append({
                    "name": service.name,
                    "version": service.version,
                    "status": status,
                    "url": url,
                    "description": service.description,
                })
            except Exception:
                services.append({
                    "name": service_dir.name,
                    "version": "unknown",
                    "status": "error",
                    "url": "",
                    "description": "Failed to load service definition",
                })

        return services

    def _get_service_status(self, name: str) -> str:
        """Get the status of a service container.

        Args:
            name: Service name (container name)

        Returns:
            Status string: running, stopped, or unknown
        """
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return "stopped"
        except Exception:
            return "unknown"

    def start(self, name: str) -> Tuple[bool, str]:
        """Start a service.

        Args:
            name: Service name

        Returns:
            Tuple of (success, message)
        """
        compose_path = self.compose_dir / f"{name}.yaml"
        if not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        return self._start_service(name, compose_path)

    def stop(self, name: str) -> Tuple[bool, str]:
        """Stop a service.

        Args:
            name: Service name

        Returns:
            Tuple of (success, message)
        """
        compose_path = self.compose_dir / f"{name}.yaml"
        if not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        return self._stop_service(name, compose_path)

    def update(self, name: str) -> Tuple[bool, str]:
        """Update a service from its git repository.

        Args:
            name: Service name

        Returns:
            Tuple of (success, message)
        """
        service_dir = self.services_dir / name
        if not service_dir.exists():
            return False, f"Service '{name}' is not installed"

        # Check if it's a git repo
        git_dir = service_dir / ".git"
        if not git_dir.exists():
            return False, f"Service '{name}' was not installed from git"

        # Get current version
        try:
            current = load_service_definition(service_dir)
            current_version = current.version
        except Exception:
            current_version = "unknown"

        # Pull latest
        try:
            result = subprocess.run(
                ["git", "-C", str(service_dir), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return False, f"Failed to update: {result.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Git pull timed out"
        except FileNotFoundError:
            return False, "Git is not installed"

        # Load updated definition
        try:
            updated = load_service_definition(service_dir)
        except Exception as e:
            return False, f"Updated service definition is invalid: {e}"

        # Regenerate compose and traefik config
        self._generate_compose_file(updated)
        try:
            domain = get_domain_from_env()
            self.traefik_config.write_config(updated, domain)
        except ValueError as e:
            return False, f"Failed to update Traefik config: {e}"

        # Restart if image changed
        if current.image != updated.image or current_version != updated.version:
            compose_path = self.compose_dir / f"{name}.yaml"
            # Pull new image
            subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "pull"],
                capture_output=True,
                timeout=120,
            )
            # Restart
            self._stop_service(name, compose_path)
            self._start_service(name, compose_path)
            return True, f"Service '{name}' updated: {current_version} -> {updated.version}"

        return True, f"Service '{name}' is up to date (v{updated.version})"
