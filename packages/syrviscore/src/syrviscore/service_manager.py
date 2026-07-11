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

from . import exposure as exposure_mod
from . import paths
from .compose_cmd import resolve_compose_cmd
from .service_schema import (
    SUBDOMAIN_RE,
    ServiceDefinition,
    ServiceValidationError,
    load_service_definition,
    validate_service_name,
)
from .traefik_config import ServiceTraefikConfig, get_domain_from_env

# Core and Layer 2 both resolve the compose command at runtime (v2 plugin or v1
# standalone), so a host with only one of them works for both.


def _image_tag(image: str) -> str:
    """Best-effort version string from a pinned image reference (for display)."""
    ref = image.split("@", 1)[0]
    if ":" in ref:
        candidate = ref.rsplit(":", 1)[1]
        if "/" not in candidate:
            return candidate
    return "0.0.0"


class ServiceManager:
    """Manage Layer 2 services for SyrvisCore."""

    def __init__(self, syrvis_home: Optional[Path] = None):
        """Initialize the service manager.

        Args:
            syrvis_home: Path to SYRVIS_HOME. Defaults to the resolved home
                (env var, then volume auto-detection) — sudo elevation strips
                SYRVIS_HOME, so a bare env lookup is not enough.
        """
        if syrvis_home:
            self.syrvis_home = Path(syrvis_home)
        else:
            self.syrvis_home = paths.get_syrvis_home()

        self.services_dir = self.syrvis_home / "services"
        self.compose_dir = self.syrvis_home / "compose"
        self.data_dir = self.syrvis_home / "data"
        # Derive the Traefik dynamic-config dir from the resolved home so the
        # manager works under sudo (which strips SYRVIS_HOME) and in tests.
        self.traefik_config = ServiceTraefikConfig(
            config_dir=self.syrvis_home / "data" / "traefik" / "config" / "dynamic"
        )

    def _service_paths(self, name: str) -> Dict[str, Path]:
        """Return the derived paths for a service, containment-checked.

        Even though names are validated at parse time, we re-validate here and
        assert every derived path stays within its parent — defense in depth
        against a name that reaches this layer unvalidated.
        """
        validate_service_name(name, "service name")
        base_dirs = {
            "service": self.services_dir,
            "data": self.data_dir,
            "compose_dir": self.compose_dir,
        }
        result = {
            "service": self.services_dir / name,
            "data": self.data_dir / name,
            "compose": self.compose_dir / "{}.yaml".format(name),
        }
        # Containment assertions: every derived path's parent must be exactly
        # the intended base directory.
        for key, base in base_dirs.items():
            target = result["compose"] if key == "compose_dir" else result[key]
            base_real = os.path.realpath(str(base))
            if key == "compose_dir":
                parent_real = os.path.realpath(str(target.parent))
            else:
                parent_real = os.path.dirname(os.path.realpath(str(target)))
            if parent_real != base_real:
                where = "compose" if key == "compose_dir" else key
                raise ServiceValidationError(
                    "Service {!r} escapes the {} directory".format(name, where)
                )
        return result

    def _project_name(self, name: str) -> str:
        """Compose project name for a service (isolates each service)."""
        return "syrvis-{}".format(name)

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        self.services_dir.mkdir(parents=True, exist_ok=True)
        self.compose_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _is_git_url(self, source: str) -> bool:
        """Check if source is a safe git URL.

        Only https, scp-style git@host:path, and ssh:// are accepted. file://,
        http://, git://, and a leading '-' are rejected: local paths and cleartext
        transports are unsafe, and a '-'-leading value could be parsed as a git
        flag. (The MCP validates more strictly still; this is the CLI's own gate.)
        """
        if not source or source.startswith("-"):
            return False
        return (
            source.startswith("https://")
            or source.startswith("git@")
            or source.startswith("ssh://")
        )

    def _clone_service(self, git_url: str) -> Tuple[bool, str, Optional[Path]]:
        """Clone a service from git.

        Args:
            git_url: Git repository URL

        Returns:
            Tuple of (success, message, service_path)
        """
        # Create temp directory for cloning
        import os
        import tempfile

        if not self._is_git_url(git_url):
            return False, f"Unsafe or unsupported git URL: {git_url!r}", None

        # Restrict the transports git may use (defense in depth against
        # protocol-helper abuse), and use '--' so a '-'-leading URL can never be
        # parsed as a git option.
        env = dict(os.environ)
        env["GIT_ALLOW_PROTOCOL"] = "https:git:ssh"
        env["GIT_TERMINAL_PROMPT"] = "0"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "repo"

            try:
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", "--", git_url, str(temp_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
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

            # Check if already installed (containment-checked target)
            try:
                target_dir = self._service_paths(service.name)["service"]
            except ServiceValidationError as e:
                return False, str(e), None
            if target_dir.exists():
                return False, f"Service '{service.name}' is already installed", None

            # Move to services directory
            self._ensure_directories()
            shutil.move(str(temp_path), str(target_dir))

            return True, f"Cloned service '{service.name}'", target_dir

    @staticmethod
    def _apply_overrides(
        service: ServiceDefinition,
        subdomain: Optional[str],
        exposure: Optional[str],
    ) -> None:
        """Apply enable-time routing overrides in place (validated)."""
        if subdomain is not None:
            sub = subdomain.strip().lower()
            if not SUBDOMAIN_RE.match(sub):
                raise ServiceValidationError(
                    "subdomain {!r} must be a single DNS label".format(subdomain)
                )
            service.traefik.enabled = True
            service.traefik.subdomain = sub
        if exposure is not None:
            service.traefik.exposure = exposure_mod.normalize(exposure)

    def add(
        self,
        source: str,
        start: bool = True,
        subdomain: Optional[str] = None,
        exposure: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Add a service from a git URL or registry name.

        Args:
            source: Git URL or registry service name
            start: Whether to start the service after adding
            subdomain: Override the manifest's Traefik subdomain (the "servicename"
                this install is routed at). Persisted into the local manifest.
            exposure: Override the manifest's exposure ("internal" | "tunnel").

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
            return False, "Registry lookup not yet implemented. Use a git URL instead."

        # Load service definition
        try:
            service = load_service_definition(service_path)
            service.source_url = source if self._is_git_url(source) else None
        except Exception as e:
            # Cleanup on failure
            if service_path and service_path.exists():
                shutil.rmtree(service_path)
            return False, f"Failed to load service: {e}"

        # Apply enable-time overrides (servicename / exposure) and persist the
        # effective manifest, so compose, Traefik, and `list()` all agree on the
        # routed name. Upstream formatting is only replaced when we override.
        if subdomain is not None or exposure is not None:
            try:
                self._apply_overrides(service, subdomain, exposure)
            except (ServiceValidationError, ValueError) as e:
                shutil.rmtree(service_path, ignore_errors=True)
                return False, "Invalid override: {}".format(e)
            (service_path / "syrvis-service.yaml").write_text(
                yaml.safe_dump(service.to_dict(), default_flow_style=False, sort_keys=False)
            )

        return self._install_from_definition(service, service_path, start)

    def add_image(
        self,
        name: str,
        image: str,
        subdomain: Optional[str] = None,
        exposure: str = exposure_mod.DEFAULT,
        port: int = 80,
        environment: Optional[List[str]] = None,
        description: str = "",
        start: bool = True,
    ) -> Tuple[bool, str]:
        """Run a Layer 2 service straight from a published image (no git repo).

        This is the "image-first" path: a caller (e.g. home-tech via the MCP)
        hands SyrvisCore an image reference plus how to route it, and SyrvisCore
        synthesizes a manifest, validates it through the same trust boundary as a
        git-sourced service, and runs it. ``exposure="tunnel"`` marks it for
        remote access — surfaced by ``syrvis stack hostnames`` for the deployment
        to reconcile (Cloudflare Tunnel route + Access).

        Args:
            name: Service name (also the container/project name).
            image: Pinned image reference (no ``:latest``), e.g. a GHCR tag.
            subdomain: Traefik subdomain to route at (defaults to ``name``).
            exposure: "internal" (LAN-only) or "tunnel" (remote via Cloudflare).
            port: Container port Traefik forwards to.
            environment: ``KEY=VALUE`` runtime env entries.
            description: Optional human description.
            start: Start the service after creating it.
        """
        self._ensure_directories()
        try:
            validate_service_name(name, "service name")
        except ServiceValidationError as e:
            return False, str(e)

        service_path = self.services_dir / name
        if service_path.exists():
            return False, "Service '{}' already exists (remove it first, or use update)".format(
                name
            )

        manifest: Dict[str, Any] = {
            "name": name,
            "version": _image_tag(image),
            "image": image,
        }
        if description:
            manifest["description"] = description
        if environment:
            manifest["environment"] = list(environment)
        manifest["traefik"] = {
            "enabled": True,
            "subdomain": (subdomain or name).strip().lower(),
            "port": port,
            "exposure": exposure,
        }

        try:
            service = ServiceDefinition.from_dict(manifest)
        except (ServiceValidationError, ValueError) as e:
            return False, "Invalid service: {}".format(e)
        service.source_url = image

        # Persist the synthesized manifest so the install is self-describing and
        # `list()` / regeneration read the same effective routing.
        service_path.mkdir(parents=True, exist_ok=True)
        (service_path / "syrvis-service.yaml").write_text(
            yaml.safe_dump(service.to_dict(), default_flow_style=False, sort_keys=False)
        )
        return self._install_from_definition(service, service_path, start)

    def _install_from_definition(
        self, service: ServiceDefinition, service_path: Path, start: bool
    ) -> Tuple[bool, str]:
        """Materialize + (optionally) start a loaded/synthesized service.

        Rolls back every artifact if any step fails, so a failed install never
        leaves partial state that blocks a retry. Shared by the git-sourced
        :meth:`add` and the image-first :meth:`add_image`.
        """
        try:
            service_data_dir = self.data_dir / service.name
            service_data_dir.mkdir(parents=True, exist_ok=True)

            if service.config_templates:
                for template in service.config_templates:
                    src = service_path / template.source
                    dest = service_data_dir / template.dest
                    if src.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)

            compose_path = self._generate_compose_file(service)

            try:
                domain = get_domain_from_env()
                self.traefik_config.write_config(service, domain)
            except ValueError as e:
                raise RuntimeError(f"Failed to configure Traefik: {e}")

            if start:
                success, msg = self._start_service(service.name, compose_path)
                if not success:
                    raise RuntimeError(f"failed to start: {msg}")
                self._reload_traefik()
                return True, f"Service '{service.name}' added and started"

            self._reload_traefik()
            return True, f"Service '{service.name}' added (not started)"
        except Exception as e:
            self._rollback_add(service.name)
            return False, f"Service '{service.name}' not added ({e})"

    def _reload_traefik(self) -> None:
        """Restart Traefik so it loads a newly written / removed L2 dynamic config.

        Traefik's file-provider watch does not reliably fire for files added to a
        subdirectory on Synology bind mounts, so a new route can sit unloaded
        until Traefik re-reads ``/config``. Restarting the container forces that.
        Best-effort: a failure here never fails the service operation (the manual
        fallback is ``docker restart traefik``).
        """
        try:
            import docker

            docker.from_env().containers.get("traefik").restart(timeout=10)
        except Exception:  # noqa: BLE001 - best-effort; never fail the op
            pass

    def _rollback_add(self, name: str) -> None:
        """Remove every artifact created for a service (best-effort)."""
        try:
            p = self._service_paths(name)
        except ServiceValidationError:
            return
        if p["compose"].exists():
            self._stop_service(name, p["compose"])
            p["compose"].unlink()
        self.traefik_config.remove_config(name)
        for path in (p["service"], p["data"]):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    def _generate_compose_file(self, service: ServiceDefinition) -> Path:
        """Generate docker-compose file for a service.

        Args:
            service: Service definition

        Returns:
            Path to generated compose file
        """
        # No top-level `version:` key — it is deprecated in Compose v2 and
        # emits a warning on every invocation.
        compose = {
            "services": {
                service.name: {
                    "image": service.image,
                    "container_name": service.container_name,
                    "restart": service.restart,
                    "networks": service.networks,
                    # Third-party services never get extra privileges.
                    "security_opt": ["no-new-privileges:true"],
                }
            },
            "networks": {
                "proxy": {
                    "external": True,
                }
            },
        }

        svc = compose["services"][service.name]

        if service.environment:
            svc["environment"] = service.environment

        # Volumes were validated by the schema (no absolute host paths, no
        # '..', no docker.sock). Every host source resolves under this
        # service's own data directory; we re-check containment here.
        if service.volumes:
            data_root = os.path.realpath(str(self.data_dir / service.name))
            processed_volumes = []
            for vol in service.volumes:
                parts = vol.split(":")
                host_path, container_path = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"

                resolved = os.path.realpath(os.path.join(data_root, host_path))
                if resolved != data_root and not resolved.startswith(data_root + os.sep):
                    raise ServiceValidationError(
                        "Volume host path {!r} escapes the service data directory".format(vol)
                    )
                processed_volumes.append(f"{resolved}:{container_path}:{mode}")

            svc["volumes"] = processed_volumes

        if service.depends_on:
            svc["depends_on"] = service.depends_on

        compose_path = self._service_paths(service.name)["compose"]
        with open(compose_path, "w") as f:
            yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

        return compose_path

    def _compose(self, name: str, compose_path: Path, *args: str, timeout: int) -> Tuple[bool, str]:
        """Run a docker compose command scoped to this service's project."""
        cmd = (
            resolve_compose_cmd()
            + [
                "-p",
                self._project_name(name),
                "-f",
                str(compose_path),
            ]
            + list(args)
        )
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                return False, result.stderr.strip()
            return True, result.stdout.strip()
        except subprocess.TimeoutExpired:
            return False, "docker compose {} timed out".format(args[0] if args else "")
        except FileNotFoundError:
            return False, "Docker is not installed"

    def _start_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        """Start a service using docker compose."""
        ok, msg = self._compose(name, compose_path, "up", "-d", timeout=120)
        return (True, "Started") if ok else (False, msg)

    def _stop_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        """Stop a service using docker compose."""
        ok, msg = self._compose(name, compose_path, "down", timeout=60)
        return (True, "Stopped") if ok else (False, msg)

    def remove(self, name: str, purge: bool = False) -> Tuple[bool, str]:
        """Remove an installed service.

        Args:
            name: Service name
            purge: If True, also remove data directory

        Returns:
            Tuple of (success, message)
        """
        try:
            p = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)

        service_dir, compose_path, data_dir = p["service"], p["compose"], p["data"]

        if not service_dir.exists() and not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        # Stop the service
        if compose_path.exists():
            self._stop_service(name, compose_path)
            compose_path.unlink()

        # Remove Traefik config + reload so the route is dropped
        self.traefik_config.remove_config(name)
        self._reload_traefik()

        # Remove service definition
        if service_dir.exists():
            shutil.rmtree(service_dir)

        # Remove data if purging
        if purge:
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

                services.append(
                    {
                        "name": service.name,
                        "version": service.version,
                        "status": status,
                        "url": url,
                        "description": service.description,
                        "subdomain": service.traefik.subdomain if service.traefik.enabled else "",
                        "exposure": (service.traefik.exposure if service.traefik.enabled else None),
                    }
                )
            except Exception:
                services.append(
                    {
                        "name": service_dir.name,
                        "version": "unknown",
                        "status": "error",
                        "url": "",
                        "description": "Failed to load service definition",
                    }
                )

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
        try:
            compose_path = self._service_paths(name)["compose"]
        except ServiceValidationError as e:
            return False, str(e)
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
        try:
            compose_path = self._service_paths(name)["compose"]
        except ServiceValidationError as e:
            return False, str(e)
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
        try:
            p = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        service_dir = p["service"]
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
            compose_path = p["compose"]
            # Pull new image (scoped to this service's compose project)
            self._compose(name, compose_path, "pull", timeout=120)
            # Restart
            self._stop_service(name, compose_path)
            self._start_service(name, compose_path)
            return True, f"Service '{name}' updated: {current_version} -> {updated.version}"

        return True, f"Service '{name}' is up to date (v{updated.version})"
