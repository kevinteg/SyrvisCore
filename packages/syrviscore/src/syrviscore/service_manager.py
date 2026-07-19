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
from . import services_d
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
            self._write_manifest(service, service_path)

        return self._install_from_definition(service, service_path, start)

    def add_image(
        self,
        name: str,
        image: str,
        subdomain: Optional[str] = None,
        exposure: str = exposure_mod.DEFAULT,
        port: int = 80,
        environment: Optional[List[str]] = None,
        volumes: Optional[List[str]] = None,
        env_file: Optional[str] = None,
        description: str = "",
        start: bool = True,
        preserve_data_on_rollback: bool = False,
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
        if volumes:
            manifest["volumes"] = list(volumes)
        if env_file:
            manifest["env_file"] = env_file
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
        self._write_manifest(service, service_path)
        return self._install_from_definition(
            service, service_path, start, preserve_data_on_rollback=preserve_data_on_rollback
        )

    def _subdomain_in_use(
        self, subdomain: str, domain: str = "", exclude: Optional[str] = None
    ) -> Optional[str]:
        """Name of an already-installed service routed at ``subdomain.domain``, else None.

        Two services claiming the same host each write a Traefik router for it, and
        Traefik's behavior for a duplicate host on one entrypoint is nondeterministic
        (last-loaded wins). Catch the collision at add time instead.

        Uniqueness is checked on the full hostname (subdomain + effective domain), not
        the subdomain alone — two services may share a subdomain on different zones
        (e.g. photos.konsume.org and photos.tegtmeier.me are distinct hosts).
        ``domain`` should be the per-service effective domain (empty string when the
        service uses the instance domain, which is fine — both sides default the same
        way so the comparison is still correct).
        """
        for info in self.list():
            if exclude and info.get("name") == exclude:
                continue
            existing_sub = info.get("subdomain") or ""
            existing_dom = info.get("domain") or ""
            if existing_sub and existing_sub == subdomain and existing_dom == domain:
                return info.get("name")
        return None

    def add_from_catalog(
        self,
        name: str,
        subdomain: Optional[str] = None,
        exposure: Optional[str] = None,
        port: Optional[int] = None,
        environment: Optional[List[str]] = None,
        start: bool = True,
    ) -> Tuple[bool, str]:
        """Install a service from a vetted catalog template (``service run <name>``
        with no ``--image``). Overrides apply on top of the template, and the
        effective manifest is persisted like every other install path.
        """
        from .catalog import CatalogError, resolve

        self._ensure_directories()
        try:
            service = resolve(name)
        except CatalogError as e:
            return False, str(e)

        service_path = self.services_dir / name
        if service_path.exists():
            return False, "Service '{}' already exists (remove it first, or use update)".format(
                name
            )

        try:
            self._apply_overrides(service, subdomain, exposure)
            if port is not None:
                if not isinstance(port, int) or not 1 <= port <= 65535:
                    raise ServiceValidationError("Invalid port {!r}: must be 1-65535".format(port))
                service.traefik.port = port
            if environment:
                service.environment = list(service.environment) + list(environment)
        except (ServiceValidationError, ValueError) as e:
            return False, "Invalid override: {}".format(e)

        service.source_url = "catalog:{}".format(name)
        service_path.mkdir(parents=True, exist_ok=True)
        self._write_manifest(service, service_path)
        return self._install_from_definition(service, service_path, start)

    def install_declaration(
        self,
        service: ServiceDefinition,
        start: bool = True,
        preserve_data_on_rollback: bool = False,
    ) -> Tuple[bool, str]:
        """Materialize a validated in-memory definition (the reconcile add/replace path).

        Phase-1 note: a declaration fully describes an image-first service. A
        git-sourced service's ``config_templates`` SOURCE files live in its
        cloned repo, so a reconcile replace re-copies templates only if the
        sources still exist; already-materialized files in data/<name>/ are
        preserved either way (remove keeps data).
        """
        self._ensure_directories()
        service_path = self.services_dir / service.name
        if service_path.exists():
            return False, "Service '{}' already exists (remove it first, or use update)".format(
                service.name
            )
        if not service.source_url:
            service.source_url = "services.d:{}".format(service.name)
        # This input IS operator/declaration-level intent (unlike a repo
        # manifest), so persist it verbatim first — the post-install dual-write
        # then preserves these very orchestration keys from the file.
        services_d.write_declaration(self.syrvis_home, service)
        service_path.mkdir(parents=True, exist_ok=True)
        self._write_manifest(service, service_path)
        return self._install_from_definition(
            service, service_path, start, preserve_data_on_rollback=preserve_data_on_rollback
        )

    def _write_manifest(self, service: ServiceDefinition, service_path: Path) -> None:
        """Persist the effective manifest, readable by the operator.

        Orchestration keys (enabled/critical) are STRIPPED: the manifest
        describes the container, orchestration lives only in services.d — and
        older service versions (rollback targets) must keep parsing manifests.

        Readability: a reconcile runs as root, so a manifest carrying inline env
        that dump_definition writes 0600 lands ``root:root`` and locks the
        operator out of ``service list`` ("Failed to load service definition").
        Give it the shared group that owns the config tree + 0640 (0644 without
        inline env) so the operator can read it while inline env stays off
        world-read. Best-effort: a non-root/edge context keeps the plain mode.
        """
        from .service_schema import dump_definition

        manifest = service_path / "syrvis-service.yaml"
        dump_definition(service, manifest, include_orchestration=False)
        try:
            shared_gid = (self.syrvis_home / "config" / "services.d").stat().st_gid
            os.chown(manifest, -1, shared_gid)  # keep owner; a root reconcile can set the group
            manifest.chmod(0o640 if service.environment else 0o644)
        except OSError:
            pass

    def _install_from_definition(
        self,
        service: ServiceDefinition,
        service_path: Path,
        start: bool,
        preserve_data_on_rollback: bool = False,
    ) -> Tuple[bool, str]:
        """Materialize + (optionally) start a loaded/synthesized service.

        Rolls back every artifact if any step fails, so a failed install never
        leaves partial state that blocks a retry. Shared by the git-sourced
        :meth:`add` and the image-first :meth:`add_image`.
        """
        # Reject a hostname already claimed by another installed service before
        # writing any Traefik config (last-writer-wins is a silent footgun).
        # Uniqueness is per full hostname (subdomain + domain); two services may
        # share a subdomain on different zones without conflict.
        if service.traefik.enabled and service.traefik.subdomain:
            owner = self._subdomain_in_use(
                service.traefik.subdomain,
                domain=service.traefik.domain,
                exclude=service.name,
            )
            if owner:
                self._rollback_add(service.name)
                effective_host = "{}.{}".format(
                    service.traefik.subdomain,
                    service.traefik.domain or "<instance-domain>",
                )
                return False, "hostname {!r} is already routed by service {!r}".format(
                    effective_host, owner
                )
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

            route_note = self._route_note(service)
            if start:
                success, msg = self._start_service(service.name, compose_path)
                if not success:
                    raise RuntimeError(f"failed to start: {msg}")
                self._reload_traefik()
                message = f"Service '{service.name}' added and started{route_note}"
            else:
                self._reload_traefik()
                message = f"Service '{service.name}' added (not started){route_note}"
        except Exception as e:
            self._rollback_add(service.name, keep_data=preserve_data_on_rollback)
            return False, f"Service '{service.name}' not added ({e})"

        # Dual-write: every successful install leaves a services.d declaration,
        # so imperative adds are visible to (and owned by) the declarative
        # layer. Strictly best-effort and OUTSIDE the rollback boundary — a
        # declaration-write failure must never tear down a running service.
        # The operator's existing orchestration keys (enabled/critical) are
        # preserved; a repo manifest can never set them.
        try:
            services_d.write_declaration_from_install(self.syrvis_home, service)
        except Exception as e:  # noqa: BLE001 - never fail the install for this
            message += " (warning: could not write services.d declaration: {})".format(e)
        return True, message

    @staticmethod
    def _route_note(service: ServiceDefinition) -> str:
        """A one-line reachability hint appended to a successful add.

        Makes the two declaration paths give honest, consistent feedback: an
        unrouted service (git manifest with no ``traefik:`` block) says so, and a
        routed one points at the exact external record ``stack hostnames`` reports.
        """
        if not service.traefik.enabled or not service.traefik.subdomain:
            return " (installed but NOT routed — no traefik block; unreachable via Traefik)"
        return (
            " — reachable once its DNS/tunnel record exists; "
            "run 'syrvis stack hostnames' for the exact record"
        )

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

    def _rollback_add(self, name: str, keep_data: bool = False) -> None:
        """Remove every artifact created for a service (best-effort).

        ``keep_data`` is set by the reconcile/converge REPLACE paths: the data
        dir there predates this install attempt, so a failed re-install must
        never destroy it (a fresh add's data dir was just created and is safe
        to drop). services.d declarations are never rollback targets — a failed
        converge keeps the declared intent so the next reconcile retries.
        """
        try:
            p = self._service_paths(name)
        except ServiceValidationError:
            return
        if p["compose"].exists():
            self._stop_service(name, p["compose"])
            p["compose"].unlink()
        self.traefik_config.remove_config(name)
        doomed = (p["service"],) if keep_data else (p["service"], p["data"])
        for path in doomed:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _ensure_volume_dir(path: Path, mode: str) -> None:
        """Create a bind-mount source dir the container can actually write to.

        Two failure modes this closes, both hit live:
        1. DSM's Docker refuses to auto-create a bind-mount source, so `up`
           fails ("Bind mount failed: ... does not exist") if the dir is absent.
        2. SyrvisCore creates the dir as the reconcile user (root over the
           operator seam), but Layer 2 images commonly run as a NON-root user
           (e.g. uid 10001) — a root-owned dir shadows the image's own volume
           dir, so the process can't write (SQLITE_CANTOPEN etc.) and crash-loops.

        The container's runtime UID is baked into the image and not reliably
        known at compose-generation time (Config.User is often a name, and the
        image may not be pulled yet), so a read-write volume is made writable by
        any UID: 0777 on the dir itself (files the container creates inside keep
        their own ownership). The dir is strictly confined under the service's
        own ``data/<name>/`` subtree (containment was just checked) on a
        single-owner NAS, so the blast radius is that one service's data. A
        read-only (``:ro``) volume needs no write bit. (A future ``user:`` schema
        field could replace this with a targeted chown.)
        """
        path.mkdir(parents=True, exist_ok=True)
        if mode != "ro":
            try:
                os.chmod(str(path), 0o777)
            except OSError:
                pass  # best-effort; a pre-owned dir may already be writable

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
                # The DSM Docker daemon refuses to auto-create a bind-mount source
                # directory, so `up` fails with "Bind mount failed: ... does not
                # exists" if we don't pre-create it (mirrors the env_file branch
                # below). Containment was just checked, so this stays under the
                # service's own data dir.
                self._ensure_volume_dir(Path(resolved), mode)
                processed_volumes.append(f"{resolved}:{container_path}:{mode}")

            svc["volumes"] = processed_volumes

        # NB: depends_on is rejected at schema-validation time (a single-service
        # compose project cannot depend on another), so it is never emitted here.

        # env_file: a data-dir-relative file holding secrets (kept out of the
        # manifest). Materialize an empty 0600 file if absent so the first
        # `up -d` doesn't fail before the operator fills it in, and clamp an
        # existing one to 0600 (it holds secrets by definition).
        if service.env_file:
            env_file_path = Path(
                os.path.realpath(str((self.data_dir / service.name) / service.env_file))
            )
            data_root = os.path.realpath(str(self.data_dir / service.name))
            if not str(env_file_path).startswith(data_root + os.sep):
                raise ServiceValidationError(
                    "env_file {!r} escapes the service data directory".format(service.env_file)
                )
            env_file_path.parent.mkdir(parents=True, exist_ok=True)
            if not env_file_path.exists():
                env_file_path.touch()
            env_file_path.chmod(0o600)
            svc["env_file"] = [str(env_file_path)]

        # Audited healthcheck subset (test/interval/timeout/retries/start_period).
        if service.healthcheck:
            svc["healthcheck"] = dict(service.healthcheck)

        # Resource guardrails: compose-spec service-level limits.
        if service.resources:
            if "cpus" in service.resources:
                svc["cpus"] = service.resources["cpus"]
            if "memory" in service.resources:
                svc["mem_limit"] = service.resources["memory"]

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

    def remove(
        self, name: str, purge: bool = False, keep_declaration: bool = False
    ) -> Tuple[bool, str]:
        """Remove an installed service.

        Args:
            name: Service name
            purge: If True, also remove data directory
            keep_declaration: Leave the services.d declaration in place (the
                reconcile REPLACE path removes only the materialization it is
                about to rebuild). Imperative removes delete the declaration
                too — otherwise the next reconcile would resurrect the service.

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

        if not keep_declaration:
            services_d.remove_declaration(self.syrvis_home, name)

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
            except Exception:
                # Only a genuine manifest-load failure is "error".
                services.append(
                    {
                        "name": service_dir.name,
                        "version": "unknown",
                        "status": "error",
                        "url": "",
                        "description": "Failed to load service definition",
                    }
                )
                continue

            # Manifest loaded — status + URL are best-effort. The unprivileged
            # operator may not reach the docker daemon or read the 0600 .env
            # (for DOMAIN); neither should turn a loadable service into an error.
            try:
                # Inspect by container_name — it defaults to the service name but
                # a manifest may override it, and the container is what has status.
                status = self._get_service_status(service.container_name or service.name)
            except Exception:
                status = "unknown"
            url = ""
            if service.traefik.enabled and service.traefik.subdomain:
                try:
                    # Use the per-service domain override when set; fall back to the instance domain.
                    effective_domain = service.traefik.domain or get_domain_from_env()
                    url = f"https://{service.traefik.subdomain}.{effective_domain}"
                except (ValueError, OSError):
                    pass

            services.append(
                {
                    "name": service.name,
                    "version": service.version,
                    "status": status,
                    "url": url,
                    "description": service.description,
                    "subdomain": service.traefik.subdomain if service.traefik.enabled else "",
                    # Per-service domain override (empty string = use instance domain).
                    # hostnames.py reads this to build the correct external hostname.
                    "domain": service.traefik.domain if service.traefik.enabled else "",
                    "exposure": (service.traefik.exposure if service.traefik.enabled else None),
                }
            )

        return services

    def _get_service_status(self, name: str) -> str:
        """Get the status of a service container.

        Uses the Docker SDK over the socket rather than shelling out to the
        `docker` binary: on Synology the binary lives at /usr/local/bin (not on a
        non-login shell's PATH), and inside the dashboard container it doesn't
        exist at all — both environments were reporting every service "unknown".
        The socket is present in both.

        Args:
            name: Container name

        Returns:
            Docker's status string (running, exited, created, paused, ...),
            "stopped" if no such container exists, or "unknown" if the Docker
            daemon can't be reached.
        """
        try:
            import docker

            try:
                return docker.from_env().containers.get(name).status
            except docker.errors.NotFound:
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
            paths_ = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        compose_path = paths_["compose"]
        manifest_path = paths_["service"] / "syrvis-service.yaml"
        if not compose_path.exists() or not manifest_path.exists():
            return False, f"Service '{name}' is not installed"

        # Re-materialize before starting so `start` self-heals host-side drift:
        # regenerating the compose file re-creates and re-permissions the
        # bind-mount volume dirs (a dir left root-owned by an older version
        # would otherwise make a non-root container crash-loop forever, with no
        # reconcile action able to fix it — `start` never regenerated compose).
        # Idempotent: identical compose content, mkdir/chmod exist_ok.
        try:
            self._generate_compose_file(load_service_definition(manifest_path))
        except Exception:  # noqa: BLE001 - fall back to the existing compose file
            pass

        ok, msg = self._start_service(name, compose_path)
        if ok:
            # Imperative start is a file author: a declared-off service becomes
            # declared-on, so the next reconcile agrees with reality.
            services_d.set_declared_enabled(self.syrvis_home, name, True)
        return ok, msg

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

        ok, msg = self._stop_service(name, compose_path)
        if ok:
            # Imperative stop = declared-but-off (enabled: false), NOT undeclared;
            # reconcile keeps it stopped instead of restarting it.
            services_d.set_declared_enabled(self.syrvis_home, name, False)
        return ok, msg

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

        # Keep the services.d declaration in step with the updated manifest
        # (content only; the operator's orchestration keys are preserved) —
        # otherwise the next reconcile would see stale intent and downgrade.
        try:
            services_d.write_declaration_from_install(self.syrvis_home, updated)
        except Exception:  # noqa: BLE001 - best-effort; update result stands
            pass

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

    # -------------------------------------------------------------------------
    # Secret management (operator-seam verb)
    # -------------------------------------------------------------------------

    _SECRET_MAX_BYTES = 65536  # 64 KiB — enough for any env-file; rejects OOM/DoS

    def write_secret(self, name: str, content: str) -> Tuple[bool, str]:
        """Write *content* to the declared service's env_file as root:root 0600.

        Security contract (mirrors env_file materialization at compose-gen time):
        - Name is re-validated (defense in depth against unvalidated callers).
        - Service must be DECLARED in config/services.d/ AND have env_file set.
        - Destination is realpath-containment-checked inside data/<name>/.
        - data/<name>/ must already exist (created by reconcile/install); we
          FAIL rather than mkdir — prevents writing secrets for undeployed svcs.
        - Write is atomic: O_CREAT|O_EXCL temp in same dir -> fsync -> os.replace.
          A dropped SSH connection or SIGKILL mid-stream never leaves a truncated
          secrets.env that would break `docker compose up`.
        - Content is capped at _SECRET_MAX_BYTES and must be non-empty.

        Returns (True, message) on success; (False, error) on any failure.
        Caller (CLI) must run as root (sudo) — ownership is root:root by default.
        """
        # --- input guards (defense in depth) --------------------------------
        if not content:
            return False, "secret content must not be empty"
        content_bytes = content.encode("utf-8", errors="surrogateescape")
        if len(content_bytes) > self._SECRET_MAX_BYTES:
            return False, (
                f"secret content too large ({len(content_bytes)} bytes; max {self._SECRET_MAX_BYTES})"
            )

        # --- name re-validation ----------------------------------------------
        try:
            paths_map = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)

        # --- declaration check (service must exist and have env_file) --------
        try:
            declarations, _invalid = services_d.load_declarations(self.syrvis_home)
        except Exception as e:  # noqa: BLE001 - catch all load failures
            return False, f"could not load service declarations: {e}"

        if name not in declarations:
            return False, f"service {name!r} is not declared in config/services.d/"

        declared = declarations[name]
        if not declared.env_file:
            return False, (
                f"service {name!r} has no env_file declared — "
                "nothing to write (set env_file in the services.d declaration)"
            )

        # --- path containment check ------------------------------------------
        data_dir_for_svc = self.data_dir / name
        env_file_path = Path(
            os.path.realpath(str(data_dir_for_svc / declared.env_file))
        )
        data_root = os.path.realpath(str(data_dir_for_svc))
        if not str(env_file_path).startswith(data_root + os.sep):
            return False, (
                f"env_file {declared.env_file!r} escapes the service data directory (path traversal)"
            )

        # --- data dir must already exist (created by reconcile/install) ------
        if not data_dir_for_svc.exists():
            return False, (
                f"data directory {data_dir_for_svc} does not exist — "
                "deploy the service first (syrvis reconcile creates it)"
            )

        # --- atomic 0600 write -----------------------------------------------
        dest = str(env_file_path)
        dest_dir = os.path.dirname(dest)
        # Ensure any sub-directory component of env_file exists (e.g. subdir/secrets.env).
        # The containment check above already verified it stays inside data/<name>/.
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        tmp = dest + f".syrvis.{os.getpid()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, dest)
            # Belt-and-braces: ensure mode is 0600 on the final path.
            os.chmod(dest, 0o600)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False, f"failed to write secret: {e}"

        return True, f"wrote {dest}"
