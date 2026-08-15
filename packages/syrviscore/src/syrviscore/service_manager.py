"""
Service manager for Layer 2 services.

Handles adding, removing, listing, and updating user-installed services.
Each service is defined by a syrvis-service.yaml file in a git repository.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import yaml

if TYPE_CHECKING:
    from .bundle import DeployBundle

from . import deployments
from . import exposure as exposure_mod
from . import jobs_d
from . import lifecycle
from . import paths
from .compose_cmd import resolve_compose_cmd
from .service_schema import (
    LOCATION_RE,
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


# design/22: the source_url prefixes that mark a service as OPERATOR-AUTHORED —
# the trust anchor for `tier: infra`. install_declaration (a config/services.d
# declaration) sets "services.d:<name>"; deploy_bundle (a bundle streamed over the
# operator seam) sets "deploy:<name>". A git-repo (add), image-first (run), or
# catalog service sets a git URL / image ref / "catalog:<name>" and therefore can
# NEVER escalate itself to the infra tier. This tuple is the whole authorship gate.
OPERATOR_AUTHORED_PREFIXES = ("services.d:", "deploy:")

# design/26: refusal message for a location change on an installed service that
# still has data — a naive replace would materialize an EMPTY home at the new
# location (Postgres would happily initdb: presents as data loss). The
# documented bypass is the app-move procedure: stop -> copy data to the new
# home -> clear the old data root -> re-declare -> deploy.
LOCATION_CHANGE_REFUSAL = (
    "location change on an installed service requires the app-move procedure "
    "(wiki/runbooks/app-move.md)"
)


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

    def _manifest_location(self, name: str) -> str:
        """The ``location:`` recorded in the CENTRAL materialized manifest, or "".

        Name-only lifecycle ops (stop/remove/logs/secret) have no definition in
        hand; the manifest at ``services/<name>/syrvis-service.yaml`` — always
        on SYRVIS_HOME, the platform's index of installed apps — is where the
        app's home is looked up.

        Fail-open vs fail-closed (adversarial review #4): only a genuinely
        ABSENT manifest means "legacy/not-installed" ("" return). Any OTHER
        read/parse failure raises — a v2 app whose manifest went corrupt must
        NEVER silently resolve to the legacy path (a purge would then rmtree
        the wrong tree while leaking the real home).
        """
        manifest = self.services_dir / name / "syrvis-service.yaml"
        try:
            text = manifest.read_text()
        except FileNotFoundError:
            return ""  # genuinely not installed / legacy-era removal in flight
        except OSError as e:
            raise ServiceValidationError(
                "Service {!r} has an unreadable materialized manifest ({}) — "
                "refusing to derive paths (fail closed); repair "
                "services/{}/syrvis-service.yaml first".format(name, e, name)
            )
        try:
            data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001 - corrupt yaml: fail closed
            raise ServiceValidationError(
                "Service {!r} has an unreadable materialized manifest ({}) — "
                "refusing to derive paths (fail closed); repair "
                "services/{}/syrvis-service.yaml first".format(name, e, name)
            )
        if not isinstance(data, dict):
            raise ServiceValidationError(
                "Service {!r} has an unreadable materialized manifest (not a "
                "mapping) — refusing to derive paths (fail closed); repair "
                "services/{}/syrvis-service.yaml first".format(name, name)
            )
        return str(data.get("location") or "")

    def _app_home(self, service_or_name) -> Optional[Path]:
        """The ONE resolution point for an app's home (design/26).

        A ServiceDefinition/dict with a truthy ``location`` resolves to
        ``<location>/syrviscore/apps/<name>``; a bare NAME reads ``location``
        from the central materialized manifest and resolves the same way.
        No location -> None (the legacy layout under SYRVIS_HOME).

        A location that reaches here MUST re-match ``^/volume\\d+$`` — a
        tampered manifest must fail CLOSED (refuse the operation), never fall
        back to a different tree that a later purge/prune would resolve wrong.
        """
        if isinstance(service_or_name, ServiceDefinition):
            name = service_or_name.name
            location = service_or_name.location or ""
        elif isinstance(service_or_name, dict):
            name = str(service_or_name.get("name") or "")
            location = str(service_or_name.get("location") or "")
        else:
            name = str(service_or_name)
            location = self._manifest_location(name)
        if not location:
            return None
        validate_service_name(name, "service name")
        if not LOCATION_RE.fullmatch(location):
            raise ServiceValidationError(
                "Service {!r} declares invalid location {!r} (must match "
                "^/volume<N>$) — refusing to derive paths from it".format(name, location)
            )
        return paths.resolve_volume_root(location) / "syrviscore" / "apps" / name

    def _service_paths(
        self, name: str, service: Optional[ServiceDefinition] = None
    ) -> Dict[str, Path]:
        """Return the derived paths for a service, containment-checked.

        Even though names are validated at parse time, we re-validate here and
        assert every derived path stays within its parent — defense in depth
        against a name that reaches this layer unvalidated.

        Layout-aware (design/26): a LEGACY service (no ``location``) returns
        exactly {service, data, compose} rooted on SYRVIS_HOME, byte-for-byte
        today's paths. A v2 app additionally carries its home and the four
        standard slots — data/config/secrets/logs under
        ``<location>/syrviscore/apps/<name>/`` — each containment-asserted
        against the app home (and the home against the apps base), so a
        symlinked home or slot can never redirect an operation elsewhere.

        ``service`` (optional) makes the DEFINITION authoritative for the home
        (install/compose-gen paths, where the manifest was just written from
        it); name-only callers resolve via the central materialized manifest.
        """
        validate_service_name(name, "service name")
        result = {
            "service": self.services_dir / name,
            "data": self.data_dir / name,
            "compose": self.compose_dir / "{}.yaml".format(name),
        }
        home = self._app_home(service if service is not None else name)
        if home is not None:
            result["home"] = home
            result["data"] = home / "data"
            result["config"] = home / "config"
            result["secrets"] = home / "secrets"
            result["logs"] = home / "logs"

        # Containment assertions: every derived path's parent must be exactly
        # the intended base directory.
        def _assert_parent(target: Path, base: Path, where: str, of_parent: bool = False) -> None:
            base_real = os.path.realpath(str(base))
            if of_parent:
                parent_real = os.path.realpath(str(target.parent))
            else:
                parent_real = os.path.dirname(os.path.realpath(str(target)))
            if parent_real != base_real:
                raise ServiceValidationError(
                    "Service {!r} escapes the {} directory".format(name, where)
                )

        _assert_parent(result["service"], self.services_dir, "service")
        _assert_parent(result["compose"], self.compose_dir, "compose", of_parent=True)
        if home is None:
            _assert_parent(result["data"], self.data_dir, "data")
        else:
            _assert_parent(home, home.parent, "apps")
            for slot in ("data", "config", "secrets", "logs"):
                _assert_parent(result[slot], home, "app home ({})".format(slot))
        return result

    def _project_name(self, name: str) -> str:
        """Compose project name for a service (isolates each service)."""
        return "syrvis-{}".format(name)

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        self.services_dir.mkdir(parents=True, exist_ok=True)
        self.compose_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_app_home(self, home: Path) -> None:
        """Materialize a v2 app home's standard layout (idempotent, adopting).

        The DSM daemon creates NOTHING (it refuses to auto-create bind-mount
        sources), so every slot is pre-created here. ``exist_ok`` semantics
        throughout: a PRE-POPULATED home (the end state of the app-move
        procedure) is silently ADOPTED — existing content is never wiped and
        never requires a flag. Modes: data/logs 0777 (the `_ensure_volume_dir`
        convention — container UID unknown), config 0755 (read-only mounts),
        secrets 0700 (root-only; env_file inside is 0600).
        """
        home.mkdir(parents=True, exist_ok=True)
        # Layout perms are UMASK-INDEPENDENT (adversarial review #8): under a
        # hardened root shell (umask 077) the mkdirs above would land 0700 and
        # a non-root container UID could not even traverse to its data dir.
        # Explicit best-effort 0755 on the home and the created ancestors
        # (<vol>/syrviscore and <vol>/syrviscore/apps) — same explicit-chmod
        # discipline every slot below gets. The volume root itself is DSM's.
        for ancestor in (home, home.parent, home.parent.parent):
            try:
                os.chmod(str(ancestor), 0o755)
            except OSError:
                pass
        self._ensure_volume_dir(home / "data", "rw")
        self._ensure_volume_dir(home / "logs", "rw")
        for slot, mode in (("config", 0o755), ("secrets", 0o700)):
            path = home / slot
            path.mkdir(exist_ok=True)
            try:
                os.chmod(str(path), mode)
            except OSError:
                pass  # best-effort (mirrors _ensure_volume_dir)

    @staticmethod
    def _dir_nonempty(path: Path) -> bool:
        """True iff ``path`` exists and contains ANY entry."""
        try:
            next(iter(path.iterdir()))
            return True
        except (OSError, StopIteration):
            return False

    def _current_data_root(self, name: str) -> Path:
        """The INSTALLED service's resolved data root (old layout or old home data/)."""
        home = self._app_home(name)
        if home is not None:
            return home / "data"
        return self.data_dir / name

    def _location_change_refusal(self, name: str, new_location: str) -> Optional[str]:
        """The design/26 immutability guard, or None when the change may proceed.

        Refuses ONLY when (installed manifest's location != incoming location)
        AND the CURRENT resolved data root exists non-empty — a naive replace
        would materialize an EMPTY home at the new location (a DB would happily
        re-init: presents as data loss). An empty/absent old data root lets the
        change proceed: that is the documented app-move bypass (stop -> copy ->
        clear old root -> re-declare -> deploy). A not-installed service (no
        manifest) trivially proceeds — fresh installs are never blocked.
        """
        old_location = self._manifest_location(name)
        if (old_location or "") == (new_location or ""):
            return None
        if self._dir_nonempty(self._current_data_root(name)):
            return LOCATION_CHANGE_REFUSAL
        return None

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
        (e.g. photos.example.com and photos.example.org are distinct hosts).
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
        *,
        trigger: str = "cli",
        previous_image: Optional[str] = None,
        record: bool = True,
        fire_hooks: bool = True,
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
            service,
            service_path,
            start,
            preserve_data_on_rollback=preserve_data_on_rollback,
            trigger=trigger,
            previous_image=previous_image,
            record=record,
            fire_hooks=fire_hooks,
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
        *,
        trigger: str = "cli",
        previous_image: Optional[str] = None,
        record: bool = True,
        fire_hooks: bool = True,
    ) -> Tuple[bool, str]:
        """Materialize + (optionally) start a loaded/synthesized service.

        Rolls back every artifact if any step fails, so a failed install never
        leaves partial state that blocks a retry. Shared by the git-sourced
        :meth:`add` and the image-first :meth:`add_image`.
        """
        # AUTHORSHIP GATE for the privileged infra tier (design/22). tier: infra
        # unlocks host mounts, so it is permitted ONLY for an operator-authored
        # declaration — services.d / a deploy bundle, which set source_url to
        # "services.d:<name>". A git-repo (add), image-first (run), or catalog
        # service — whose source_url is the git URL / image ref / "catalog:…" —
        # can NEVER escalate itself to infra. This is the load-bearing rule of
        # design/22 (the schema accepts tier:infra when parsing so a materialized
        # manifest round-trips; the trust decision is made HERE, by authorship).
        if service.tier == "infra" and not str(service.source_url or "").startswith(
            OPERATOR_AUTHORED_PREFIXES
        ):
            # keep_data=True (adversarial review, release-blocker #1): the data
            # dir is only created further down, so anything at the data
            # location right now PREDATES this call (e.g. data kept by a
            # remove-without-purge, or a pre-populated v2 home) — a gate
            # refusal must never destroy it. Same rule as the location gate.
            self._rollback_add(service.name, keep_data=True)
            return False, (
                "tier: infra is only permitted for an operator-authored "
                "services.d/deploy declaration — not a {} service".format(
                    service.source_url or "repo/image"
                )
            )
        # AUTHORSHIP GATE for the app location (design/26) — the same trust
        # anchor as tier: a location redirects the app's home onto a host
        # volume, so it is permitted ONLY for an operator-authored services.d/
        # deploy declaration. A git-repo, image-first, or catalog manifest that
        # carries one is refused here. keep_data=True: nothing at the (declared
        # or legacy) data location was created by THIS call yet, so an abort
        # must never destroy what may already be there.
        if service.location:
            if not str(service.source_url or "").startswith(OPERATOR_AUTHORED_PREFIXES):
                self._rollback_add(service.name, keep_data=True)
                return False, (
                    "location: is only permitted for an operator-authored "
                    "services.d/deploy declaration — not a {} service".format(
                        service.source_url or "repo/image"
                    )
                )
            # Operator-authored: the value must (still) be a volume root AND a
            # real mounted volume — deploying onto an unmounted /volumeN would
            # materialize the app home on the bare root filesystem.
            if not LOCATION_RE.fullmatch(service.location):
                self._rollback_add(service.name, keep_data=True)
                return False, (
                    "invalid location {!r}: must match ^/volume<N>$".format(service.location)
                )
            if not paths.is_mounted_volume(service.location):
                self._rollback_add(service.name, keep_data=True)
                return False, (
                    "location {!r} is not a mounted volume — refusing to "
                    "materialize the app home there".format(service.location)
                )
        # pre-deploy host hook: the one abortable deploy gate (e.g. a backup
        # taken before every rollout). The declaration/manifest were already
        # written — a persistent hook failure surfaces as an isolated,
        # retryable reconcile failure for this service. keep_data=True: the
        # data dir is only created further down (:569), so anything at
        # data/<name>/ right now predates this call (e.g. a remove without
        # --purge kept it) and an abort must never destroy it.
        if fire_hooks:
            hooks_ok, runs = self._fire_hooks(
                service,
                "pre-deploy",
                context={"image": service.image, "previous_image": previous_image or ""},
            )
            if not hooks_ok:
                self._rollback_add(service.name, keep_data=True)
                return False, "deploy aborted by pre-deploy hook: " + self._hook_failure_summary(
                    runs
                )

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
                # keep_data=True for the same reason as the hook abort above:
                # the data dir was not created yet by this invocation.
                self._rollback_add(service.name, keep_data=True)
                effective_host = "{}.{}".format(
                    service.traefik.subdomain,
                    service.traefik.domain or "<instance-domain>",
                )
                return False, "hostname {!r} is already routed by service {!r}".format(
                    effective_host, owner
                )
        # A v2 home that ALREADY has content (the end state of the app-move
        # procedure) is silently adopted — and must then survive a failed
        # install's rollback exactly like reconcile-REPLACE data does: it
        # predates this call, so the rollback may never destroy it. The
        # predicate is the WHOLE home (adversarial review #6): a home
        # pre-staged with only config/ (no data yet) is just as much not-ours-
        # to-delete. Evaluated BEFORE _ensure_app_home creates any slot.
        home = self._app_home(service)
        adopted = home is not None and self._dir_nonempty(home)
        try:
            if home is not None:
                # Containment (incl. the four slots) BEFORE creating/adopting:
                # a pre-planted symlinked slot must fail the install, not
                # redirect template copies or mounts.
                self._service_paths(service.name, service)
                self._ensure_app_home(home)
                service_data_dir = home / "data"
                config_root = home / "config"
            else:
                service_data_dir = self.data_dir / service.name
                service_data_dir.mkdir(parents=True, exist_ok=True)
                config_root = service_data_dir

            if service.config_templates:
                for template in service.config_templates:
                    src = service_path / template.source
                    dest = config_root / template.dest
                    if src.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)

            compose_path = self._generate_compose_file(service)

            try:
                domain = get_domain_from_env()
                _, traefik_changed = self.traefik_config.write_config(service, domain)
            except ValueError as e:
                raise RuntimeError(f"Failed to configure Traefik: {e}")

            route_note = self._route_note(service)
            if start:
                success, msg = self._start_service(service.name, compose_path)
                if not success:
                    raise RuntimeError(f"failed to start: {msg}")
                if traefik_changed:
                    self._reload_traefik()
                message = f"Service '{service.name}' added and started{route_note}"
            else:
                if traefik_changed:
                    self._reload_traefik()
                message = f"Service '{service.name}' added (not started){route_note}"
        except Exception as e:
            self._rollback_add(service.name, keep_data=preserve_data_on_rollback or adopted)
            # A failed reconcile REPLACE left changed state (the old service was
            # already torn down) — history must show the failed rollout, like
            # set_image/deploy_bundle do. A fully-rolled-back FRESH add changed
            # nothing and records nothing.
            if record and preserve_data_on_rollback:
                deployments.record_service_deploy(
                    self.syrvis_home,
                    service,
                    action="deploy",
                    trigger=trigger,
                    outcome="failed",
                    previous_image=previous_image,
                    detail=f"Service '{service.name}' not added ({e})",
                )
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
        # The one materialization choke point records the deployment (add/run/
        # catalog/reconcile add+replace). deploy_bundle passes record=False and
        # records once at its own end, after configs/secrets land.
        if record:
            deployments.record_service_deploy(
                self.syrvis_home,
                service,
                action="deploy",
                trigger=trigger,
                outcome="success",
                previous_image=previous_image,
                detail=message,
            )
        if fire_hooks:
            self._fire_hooks(
                service,
                "post-deploy",
                force=True,
                context={"image": service.image, "previous_image": previous_image or ""},
            )
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

        Call this ONLY when write_config/remove_config reported a real change —
        an unconditional call at the end of every deploy turned multi-service
        stack sweeps into an edge-proxy restart storm (39 restarts/day). The 30s
        timeout must stay ABOVE the static config's graceTimeOut (20s) so the
        drain finishes and Traefik exits cleanly instead of panicking.
        Best-effort: a failure here never fails the service operation (the manual
        fallback is ``docker restart traefik``).
        """
        try:
            import docker

            docker.from_env().containers.get("traefik").restart(timeout=30)
        except Exception as e:  # noqa: BLE001 - best-effort; never fail the op
            import logging

            logging.getLogger(__name__).warning("traefik reload failed: %s", e)

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
        if self.traefik_config.remove_config(name):
            self._reload_traefik()
        # A v2 app's data-bearing artifact is its whole HOME (self-contained
        # unit): dropping only home/data would leave config/secrets orphaned
        # at the location root. Legacy keeps today's data/<name> target.
        data_target = p.get("home") or p["data"]
        doomed = (p["service"],) if keep_data else (p["service"], data_target)
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

        # Container command override (argv / exec form). The schema validated it
        # as a non-empty list of literal strings (no shell, no '$'), so emit it
        # verbatim: it parameterizes the image's ENTRYPOINT and runs under the
        # same confinement (no-new-privileges, no caps, no host mounts) as
        # every other Layer 2 service. Needed by argv-driven images such as
        # VictoriaMetrics' vmagent/vmalert, which have no env-var-only config.
        if service.command:
            svc["command"] = list(service.command)

        # Volumes were validated by the schema. For a normal service every host
        # source resolves under its own data directory (re-checked here). An
        # `infra`-tier service (design/22) may ALSO carry an enumerated read-only
        # host mount (/proc, /sys, /, docker.sock) — those pass through as the
        # absolute host path, not resolved under data/<svc>/ and not pre-created
        # (they already exist). The authorship gate that only an operator may set
        # tier: infra was enforced at install time.
        # design/26 (adversarial review, release-blocker #2): the mount check
        # is NOT install-time-only. Every start/up/update path shares this
        # choke point, and it must refuse BEFORE creating anything — otherwise
        # a boot/start while the declared volume is unmounted would silently
        # re-materialize an EMPTY home on the bare mountpoint (a DB would
        # happily initdb there: presents as data loss, then hides under the
        # real volume when it mounts later).
        if service.location and not paths.is_mounted_volume(service.location):
            raise ServiceValidationError(
                "location {!r} is not a mounted volume — refusing to "
                "materialize or start service {!r} (mount the volume, then "
                "retry)".format(service.location, service.name)
            )

        # Layout-aware roots (design/26): the definition is authoritative here —
        # the materialized manifest was just written from it on every install/
        # update path. Legacy: both roots are data/<name> (byte-for-byte
        # today's behavior). v2: volumes resolve under home/data, the env_file
        # under home/secrets. Slot containment was asserted by _service_paths.
        paths_ = self._service_paths(service.name, service)
        is_v2 = "home" in paths_
        data_root_dir = paths_["data"]
        secrets_root_dir = paths_.get("secrets") or data_root_dir

        if service.volumes:
            from .service_schema import FILEPLANE_PREFIX, INFRA_HOST_MOUNTS

            is_infra = service.tier == "infra"
            data_root = os.path.realpath(str(data_root_dir))
            processed_volumes = []
            shares_cache = None
            for vol in service.volumes:
                if vol.startswith(FILEPLANE_PREFIX):
                    # File-plane bind (fileplane=<share>[/<subpath>]:<mount>:<mode>):
                    # resolve the declared share (shares.d) to its host path. The
                    # engine NEVER creates or chmods file-plane directories — that
                    # content is operator-owned; a missing path is a loud error,
                    # not a mkdir (the 0777 any-UID treatment data dirs get would
                    # be a security regression on a family share).
                    from .shares_registry import (
                        SharesRegistryError,
                        load_shares,
                        resolve_fileplane,
                    )

                    body = vol[len(FILEPLANE_PREFIX) :]
                    source, container_path, mode = body.split(":")
                    share_id, _, subpath = source.partition("/")
                    if shares_cache is None:
                        shares_cache = load_shares(self.syrvis_home)
                    host_path = resolve_fileplane(
                        shares_cache, service.name, share_id, subpath, mode
                    )
                    if not os.path.isdir(host_path):
                        raise SharesRegistryError(
                            "File-plane path {!r} (share {!r}) does not exist on "
                            "this host — file-plane content is operator-managed; "
                            "the engine will not create it".format(host_path, share_id)
                        )
                    processed_volumes.append("{}:{}:{}".format(host_path, container_path, mode))
                    continue
                parts = vol.split(":")
                host_path, container_path = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"

                if is_infra and host_path in INFRA_HOST_MOUNTS:
                    # Belt-and-braces: only ever read-only, only the allowlist.
                    processed_volumes.append(f"{host_path}:{container_path}:ro")
                    continue

                # v2 SLOT MAPPING (design/26, owner-decided): the standard
                # layout is SEMANTIC for a located app. A relative source named
                # exactly "config" mounts home/config (so the container reads
                # exactly what _place_config wrote) and exactly "logs" mounts
                # home/logs; every other relative source stays under home/data.
                # "secrets" is REFUSED — env_file is the one secrets mechanism;
                # a secrets bind-mount would bypass the 0700/0600 discipline.
                # Legacy services never enter this branch (everything resolves
                # under data/<svc> exactly as today).
                if is_v2 and host_path == "secrets":
                    raise ServiceValidationError(
                        "Volume {!r}: a located app may not bind-mount the "
                        "secrets slot — secrets flow through env_file".format(vol)
                    )
                if is_v2 and host_path in ("config", "logs"):
                    # Containment for the mapped slot was asserted against the
                    # app home by _service_paths (parent(realpath(slot)) ==
                    # realpath(home)); the source is a fixed name, so there is
                    # no path component to traverse with.
                    slot_dir = Path(os.path.realpath(str(paths_[host_path])))
                    if host_path == "logs":
                        self._ensure_volume_dir(slot_dir, mode)
                    else:
                        # config keeps the slot convention (0755) — never the
                        # 0777 any-UID-writable treatment data dirs get.
                        slot_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            os.chmod(str(slot_dir), 0o755)
                        except OSError:
                            pass
                    processed_volumes.append(f"{slot_dir}:{container_path}:{mode}")
                    continue

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

        if service.ports:
            # Raw host port publishes (non-HTTP data planes — sync protocols
            # etc.). Declared intent, schema-validated 'H:C/proto'.
            svc["ports"] = list(service.ports)

        # NB: depends_on is rejected at schema-validation time (a single-service
        # compose project cannot depend on another), so it is never emitted here.

        # env_file: a secrets file kept out of the manifest (legacy: relative to
        # data/<name>; v2: relative to home/secrets). Materialize an empty 0600
        # file if absent so the first `up -d` doesn't fail before the operator
        # fills it in, and clamp an existing one to 0600 (it holds secrets by
        # definition).
        if service.env_file:
            env_file_path = Path(os.path.realpath(str(secrets_root_dir / service.env_file)))
            env_root = os.path.realpath(str(secrets_root_dir))
            if not str(env_file_path).startswith(env_root + os.sep):
                raise ServiceValidationError(
                    "env_file {!r} escapes the service {} directory".format(
                        service.env_file,
                        "secrets" if "secrets" in paths_ else "data",
                    )
                )
            env_file_path.parent.mkdir(parents=True, exist_ok=True)
            if not env_file_path.exists():
                env_file_path.touch()
            env_file_path.chmod(0o600)
            svc["env_file"] = [str(env_file_path)]

        # Audited healthcheck subset (test/interval/timeout/retries/start_period).
        if service.healthcheck:
            svc["healthcheck"] = dict(service.healthcheck)

        # Graceful-stop grace (SIGTERM -> SIGKILL window). Emitting it into the
        # compose spec makes the grace durable on EVERY stop path — including
        # the docker daemon's own stop at OS poweroff — not just syrvis verbs.
        if service.shutdown and service.shutdown.get("stop_timeout"):
            svc["stop_grace_period"] = "{}s".format(service.shutdown["stop_timeout"])

        # Resource guardrails: compose-spec service-level limits.
        if service.resources:
            if "cpus" in service.resources:
                svc["cpus"] = service.resources["cpus"]
            if "memory" in service.resources:
                svc["mem_limit"] = service.resources["memory"]

        compose_path = paths_["compose"]
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

    # Best-effort image pre-pull budget before the timed `up -d` (design/26
    # companion fix): a very large image (multi-GB model servers) cannot pull
    # inside up's 120s window. 900s covers the pathological first pull; pull
    # failure is logged-not-fatal because `up` is authoritative (it pulls
    # whatever is still missing, and an already-present image needs neither).
    _PULL_TIMEOUT_S = 900

    @staticmethod
    def _compose_image(name: str, compose_path: Path) -> Optional[str]:
        """The image ref the generated compose file pins for ``name`` (best-effort)."""
        try:
            data = yaml.safe_load(Path(compose_path).read_text())
            image = data["services"][name]["image"]
            return image if isinstance(image, str) and image else None
        except Exception:  # noqa: BLE001 - absent/foreign compose: undetermined
            return None

    @staticmethod
    def _image_present_locally(image: str) -> bool:
        """True iff the pinned image already exists in the local docker store."""
        try:
            import docker

            docker.from_env().images.get(image)
            return True
        except Exception:  # noqa: BLE001 - not found / no daemon: not present
            return False

    def _start_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        """Start a service using docker compose (best-effort pre-pull first).

        The pre-pull is SKIPPED when the pinned image is already local
        (adversarial review #7): every routine start (boot resume, reconcile)
        must not touch the registry — with a blackholed registry, 19 services
        x 900s would stall a resume for hours. The 900s budget applies only to
        a genuinely-missing image (the huge-first-pull case it exists for);
        an undetermined image errs toward pulling (pull is best-effort anyway).
        """
        image = self._compose_image(name, compose_path)
        if image and self._image_present_locally(image):
            return self._up_service(name, compose_path)
        pull_ok, pull_msg = self._compose(name, compose_path, "pull", timeout=self._PULL_TIMEOUT_S)
        if not pull_ok:
            import logging

            logging.getLogger(__name__).warning(
                "pre-pull for service %r failed (%s) — proceeding to up", name, pull_msg
            )
        return self._up_service(name, compose_path)

    def _up_service(self, name: str, compose_path: Path) -> Tuple[bool, str]:
        ok, msg = self._compose(name, compose_path, "up", "-d", timeout=120)
        return (True, "Started") if ok else (False, msg)

    def _stop_service(
        self,
        name: str,
        compose_path: Path,
        remove: bool = True,
        stop_timeout: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Stop a service using docker compose.

        ``remove=True`` (the default, all existing paths) runs ``down`` —
        containers are removed. ``remove=False`` (the graceful-shutdown path)
        runs ``stop -t <grace>`` — containers stay for a fast ``resume``.
        """
        if remove:
            ok, msg = self._compose(name, compose_path, "down", timeout=60)
        else:
            grace = int(stop_timeout or 30)
            ok, msg = self._compose(
                name, compose_path, "stop", "-t", str(grace), timeout=grace + 30
            )
        return (True, "Stopped") if ok else (False, msg)

    # -------------------------------------------------------------------------
    # Lifecycle transition hooks
    # -------------------------------------------------------------------------

    _HOOK_TIMEOUT_S = lifecycle.HOOK_DEFAULT_TIMEOUT_S

    def _run_container_hook(
        self, service: ServiceDefinition, event: str, timeout: int
    ) -> "lifecycle.HookRun":
        """Run the manifest's declared container hook for ``event`` (if any).

        No new trust surface: the argv comes only from the schema-audited
        ``tasks:`` map and runs in the service's own container.
        """
        run = lifecycle.HookRun(
            kind="container", event=event, scope="service", workload=service.name
        )
        task = (service.hooks or {}).get(event)
        if not task:
            return run
        run.ran = True
        ok, output = self._docker_exec_task(service, service.tasks[task], timeout)
        run.ok = ok
        run.rc = 0 if ok else 1
        run.output_tail = output[-lifecycle.HOOK_OUTPUT_CAP :]
        return run

    def _fire_hooks(
        self,
        service_or_name,
        event: str,
        force: bool = False,
        timeout: Optional[int] = None,
        reason: str = "",
        context: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, List["lifecycle.HookRun"]]:
        """Fire the container hook (when valid) then the host hook for an event.

        Returns (ok, runs); ``ok`` is advisory — only ABORTING_EVENTS callers
        act on it, and never under ``force`` (the UPS path never blocks).
        """
        if os.environ.get("SYRVIS_IN_HOOK"):
            return True, []
        t = int(timeout or self._HOOK_TIMEOUT_S)
        runs: List[lifecycle.HookRun] = []
        if event in lifecycle.CONTAINER_HOOK_EVENTS and isinstance(
            service_or_name, ServiceDefinition
        ):
            runs.append(self._run_container_hook(service_or_name, event, t))
        name = (
            service_or_name.name
            if isinstance(service_or_name, ServiceDefinition)
            else str(service_or_name)
        )
        runs.append(
            lifecycle.run_host_hook(
                self.syrvis_home, event, "service", name, reason=reason, context=context, timeout=t
            )
        )
        ok = all(r.ok for r in runs) or force
        return ok, runs

    @staticmethod
    def _hook_failure_summary(runs: List["lifecycle.HookRun"]) -> str:
        failed = [r for r in runs if not r.ok]
        return "; ".join(
            "{} {} hook failed: {}".format(r.kind, r.event, (r.output_tail or "").strip()[-200:])
            for r in failed
        )

    def remove(
        self,
        name: str,
        purge: bool = False,
        keep_declaration: bool = False,
        *,
        trigger: str = "cli",
        fire_hooks: bool = True,
    ) -> Tuple[bool, str]:
        """Remove an installed service.

        Args:
            name: Service name
            purge: If True, also remove data directory
            keep_declaration: Leave the services.d declaration in place (the
                reconcile REPLACE path removes only the materialization it is
                about to rebuild). Imperative removes delete the declaration
                too — otherwise the next reconcile would resurrect the service.
            trigger: recorded in the deployment history ("cli"/"reconcile"/...).

        Returns:
            Tuple of (success, message)
        """
        try:
            p = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)

        service_dir, compose_path = p["service"], p["compose"]
        # Purge target: a v2 app's data-bearing artifact is its whole HOME
        # (self-contained unit — leaving config/secrets behind would orphan
        # them at the location root); legacy purges data/<name> as today.
        data_dir = p.get("home") or p["data"]

        if not service_dir.exists() and not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        # Capture the removed image for the history record (best-effort).
        removed_image: Optional[str] = None
        removed_svc: Optional[ServiceDefinition] = None
        try:
            removed_svc = load_service_definition(service_dir)
            removed_image = removed_svc.image
        except Exception:  # noqa: BLE001 - broken manifest; record without it
            pass

        # Stop the service (pre-stop quiesce fires but never blocks a removal;
        # the reconcile-REPLACE teardown passes fire_hooks=False so a replace
        # fires only deploy events, never a spurious double-quiesce).
        if compose_path.exists():
            if fire_hooks:
                self._fire_hooks(removed_svc or name, "pre-stop", force=True)
            self._stop_service(name, compose_path)
            if fire_hooks:
                self._fire_hooks(removed_svc or name, "post-stop", force=True)
            compose_path.unlink()

        # Remove Traefik config + reload so the route is dropped (no reload
        # needed for a service that never had one)
        if self.traefik_config.remove_config(name):
            self._reload_traefik()

        # Remove service definition
        if service_dir.exists():
            shutil.rmtree(service_dir)

        if not keep_declaration:
            services_d.remove_declaration(self.syrvis_home, name)
            # keep_declaration=True is the reconcile-REPLACE teardown: the
            # following install records the single logical deploy. Only a real
            # removal is history.
            deployments.record_service_remove(
                self.syrvis_home, name, trigger=trigger, previous_image=removed_image
            )

        # Remove data if purging (the deployment history under data/deployments/
        # survives a purge — it is the removal's audit trail).
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
                        "location": "",
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
                    # The app's declared home volume ("" = legacy layout on
                    # SYRVIS_HOME). Flows to the seam/dashboard rows as-is.
                    "location": service.location or "",
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

    def start(
        self,
        name: str,
        fire_hooks: bool = True,
        force: bool = False,
        hook_timeout: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Start a service.

        Fires the ``pre-start`` host hook (aborting unless ``force``) and, on
        success, the ``post-start`` hooks (container + host, log-only).

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
        svc: Optional[ServiceDefinition] = None
        try:
            svc = load_service_definition(manifest_path)
            self._generate_compose_file(svc)
        except ServiceValidationError as e:
            # Fail CLOSED: a REFUSED re-materialization (unmounted location,
            # containment breach) must refuse the start — never fall back to a
            # stale compose file that points at the wrong (or empty) tree.
            return False, str(e)
        except Exception:  # noqa: BLE001 - fall back to the existing compose file
            pass

        if fire_hooks:
            hooks_ok, runs = self._fire_hooks(
                svc or name, "pre-start", force=force, timeout=hook_timeout
            )
            if not hooks_ok:
                return False, "start aborted by pre-start hook: " + self._hook_failure_summary(runs)

        ok, msg = self._start_service(name, compose_path)
        if ok:
            # Imperative start is a file author: a declared-off service becomes
            # declared-on, so the next reconcile agrees with reality.
            services_d.set_declared_enabled(self.syrvis_home, name, True)
            if fire_hooks:
                self._fire_hooks(svc or name, "post-start", force=True, timeout=hook_timeout)
        return ok, msg

    def stop(
        self,
        name: str,
        remove: bool = True,
        set_intent: bool = True,
        fire_hooks: bool = True,
        force: bool = False,
        hook_timeout: Optional[int] = None,
        stop_timeout: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Stop a service.

        The default is the imperative verb: ``compose down`` + flip the
        declared ``enabled`` flag (an intent change). The graceful-shutdown
        path passes ``remove=False, set_intent=False`` — an operational stop
        (containers kept, intent untouched, so resume/reconcile restores it).
        ``pre-stop`` hooks (container quiesce first, then host) fire before
        the stop and NEVER abort it; ``post-stop`` (host) fires after.

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
        if not compose_path.exists():
            return False, f"Service '{name}' is not installed"

        svc: Optional[ServiceDefinition] = None
        try:
            svc = load_service_definition(paths_["service"] / "syrvis-service.yaml")
        except Exception:  # noqa: BLE001 - container hook skipped, host still fires
            pass

        if fire_hooks:
            # Stop must always be possible: pre-stop failures log and proceed.
            self._fire_hooks(svc or name, "pre-stop", force=True, timeout=hook_timeout)

        if stop_timeout is None and svc is not None and svc.shutdown:
            stop_timeout = svc.shutdown.get("stop_timeout")
        ok, msg = self._stop_service(name, compose_path, remove=remove, stop_timeout=stop_timeout)
        if ok:
            if set_intent:
                # Imperative stop = declared-but-off (enabled: false), NOT
                # undeclared; reconcile keeps it stopped instead of restarting.
                services_d.set_declared_enabled(self.syrvis_home, name, False)
            if fire_hooks:
                self._fire_hooks(svc or name, "post-stop", force=True, timeout=hook_timeout)
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

        # Get current (INSTALLED) definition — the trust baseline the gate
        # below compares against. Loaded BEFORE the pull: for a git service the
        # materialized manifest IS the repo file, so after the pull it already
        # holds the candidate content.
        current: Optional[ServiceDefinition] = None
        try:
            current = load_service_definition(service_dir)
            current_version = current.version
        except Exception:
            current_version = "unknown"

        # Remember the pre-pull commit so a refused update can be reverted —
        # otherwise the on-disk manifest would already carry the refused
        # tier/location and every later name-only path lookup would honor it.
        pre_pull_head = ""
        try:
            head = subprocess.run(
                ["git", "-C", str(service_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if head.returncode == 0:
                pre_pull_head = head.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

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

        # AUTHORSHIP GATE, update path (design/22 gap fix + design/26): a
        # git-pull update is by definition NOT operator-authored (this verb
        # requires a .git working tree), so it may never CHANGE the privileged
        # fields — tier (host mounts) or location (the app's home volume).
        # Both fields, one code path. On refusal the on-disk baseline is
        # RESTORED (adversarial review #3/#6): the manifest is re-materialized
        # from the in-memory ``current`` (the last-AUTHORIZED definition), so a
        # second update can never launder the change by comparing against a
        # polluted on-disk manifest; the git working tree is additionally reset
        # (returncode CHECKED) so pulled non-manifest files don't linger.
        installed_tier = current.tier if current is not None else ""
        installed_location = current.location if current is not None else ""
        for field_name, old, new in (
            ("tier", installed_tier, updated.tier),
            ("location", installed_location, updated.location),
        ):
            if old != new:
                git_reverted = False
                if pre_pull_head:
                    try:
                        reset = subprocess.run(
                            ["git", "-C", str(service_dir), "reset", "--hard", pre_pull_head],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        git_reverted = reset.returncode == 0
                    except (subprocess.TimeoutExpired, OSError):
                        git_reverted = False
                # The load-bearing restore: the MANIFEST (the gate's baseline
                # and every name-only path lookup's truth) comes back from the
                # last-authorized in-memory copy, independent of git state.
                manifest_restored = False
                if current is not None:
                    try:
                        self._write_manifest(current, service_dir)
                        manifest_restored = True
                    except OSError:
                        manifest_restored = False
                base_msg = (
                    "update changes {} ({!r} -> {!r}): {} is only permitted for an "
                    "operator-authored services.d/deploy declaration — a git-sourced "
                    "update cannot change it".format(field_name, old, new, field_name)
                )
                if git_reverted and manifest_restored:
                    return False, base_msg + " (pull reverted)"
                if manifest_restored:
                    return False, base_msg + (
                        " — git REVERT FAILED; the installed manifest was restored "
                        "from the last-authorized copy, but the git working tree at "
                        "services/{} may still hold the refused change — manual "
                        "intervention required".format(name)
                    )
                return False, base_msg + (
                    " — REVERT FAILED; the refused manifest may still be the on-disk "
                    "truth at services/{} — manual intervention required before any "
                    "further operation on this service".format(name)
                )

        hooks_ok, runs = self._fire_hooks(
            updated,
            "pre-deploy",
            context={
                "image": updated.image,
                "previous_image": current.image if current is not None else "",
            },
        )
        if not hooks_ok:
            return False, "update aborted by pre-deploy hook: " + self._hook_failure_summary(runs)

        # Regenerate compose and traefik config. Reload on a real route change —
        # previously an update NEVER reloaded, so a changed traefik: block kept
        # serving the old route until something else bounced the proxy.
        self._generate_compose_file(updated)
        try:
            domain = get_domain_from_env()
            _, traefik_changed = self.traefik_config.write_config(updated, domain)
        except ValueError as e:
            return False, f"Failed to update Traefik config: {e}"
        if traefik_changed:
            self._reload_traefik()

        # Keep the services.d declaration in step with the updated manifest
        # (content only; the operator's orchestration keys are preserved) —
        # otherwise the next reconcile would see stale intent and downgrade.
        try:
            services_d.write_declaration_from_install(self.syrvis_home, updated)
        except Exception:  # noqa: BLE001 - best-effort; update result stands
            pass

        # Restart if image changed (an unreadable prior manifest counts as changed)
        current_image = current.image if current is not None else None
        if current_image != updated.image or current_version != updated.version:
            compose_path = p["compose"]
            # Pull new image (scoped to this service's compose project)
            self._compose(name, compose_path, "pull", timeout=120)
            # Restart
            self._stop_service(name, compose_path)
            self._start_service(name, compose_path)
            deployments.record_service_deploy(
                self.syrvis_home,
                updated,
                action="deploy",
                trigger="cli",
                outcome="success",
                previous_image=current_image,
            )
            self._fire_hooks(
                updated,
                "post-deploy",
                force=True,
                context={"image": updated.image, "previous_image": current_image or ""},
            )
            return True, f"Service '{name}' updated: {current_version} -> {updated.version}"

        return True, f"Service '{name}' is up to date (v{updated.version})"

    def set_image(self, name: str, new_image: str) -> Tuple[bool, str]:
        """Re-pin an installed Layer 2 service to ``new_image`` and redeploy.

        The declarative apply path for a container-image update (the companion
        to ``syrvis updates``): swap the manifest's ``image:``, re-validate it
        through the full ServiceDefinition trust boundary (so the new ref must
        be a pinned, audited image), regenerate compose + the Traefik file,
        dual-write the services.d declaration so a later reconcile agrees, then
        pull + restart. Git-based services keep updating via ``service update``
        (their image comes from the repo).
        """
        try:
            p = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        service_dir = p["service"]
        manifest_path = service_dir / "syrvis-service.yaml"
        if not manifest_path.exists():
            return False, f"Service '{name}' is not installed"
        if (service_dir / ".git").exists():
            return False, (
                f"'{name}' was installed from git — update it with "
                "'syrvis service update' (its image comes from the repo)"
            )
        try:
            current = load_service_definition(service_dir)
        except Exception as e:  # noqa: BLE001
            return False, f"could not load manifest for '{name}': {e}"

        if new_image == current.image:
            return True, f"Service '{name}' already pinned to {new_image}"

        # Rebuild through the trust boundary: only the image + derived version
        # change; everything else (volumes, env, traefik, tier) is preserved and
        # re-validated. A bad/unpinned ref is rejected here before anything runs.
        data = current.to_dict()
        data["image"] = new_image
        data["version"] = _image_tag(new_image)
        try:
            updated = ServiceDefinition.from_dict(data)
        except ServiceValidationError as e:
            return False, f"invalid image {new_image!r}: {e}"

        # design/26: location is immutable while installed (by construction the
        # round-trip above preserves it, but the guard keeps future edits to
        # this path honest — a re-pin must never relocate an app's home).
        if updated.location != current.location and self._dir_nonempty(
            self._current_data_root(name)
        ):
            return False, LOCATION_CHANGE_REFUSAL

        old_image = current.image

        hooks_ok, runs = self._fire_hooks(
            updated, "pre-deploy", context={"image": new_image, "previous_image": old_image}
        )
        if not hooks_ok:
            return False, "re-pin aborted by pre-deploy hook: " + self._hook_failure_summary(runs)

        # Pull the NEW image FIRST — before touching the manifest or the running
        # container. A valid-format but non-existent/unpullable tag (a typo, a
        # registry blip) must NOT tear down a healthy service: from_dict only
        # checks the ref's FORMAT, not that it exists. If the pull fails we abort
        # with the old image still installed and running.
        try:
            pull = subprocess.run(
                ["docker", "pull", new_image],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return False, f"pulling {new_image} timed out — service left on {old_image}"
        except OSError as e:
            return False, f"could not run docker pull: {e}"
        if pull.returncode != 0:
            detail = (pull.stderr or pull.stdout or "").strip().splitlines()[-1:] or [""]
            return False, (
                f"could not pull {new_image} ({detail[0]}) — service left unchanged on {old_image}"
            )

        self._write_manifest(updated, service_dir)
        self._generate_compose_file(updated)
        try:
            domain = get_domain_from_env()
            _, traefik_changed = self.traefik_config.write_config(updated, domain)
        except ValueError as e:
            return False, f"Failed to update Traefik config: {e}"
        if traefik_changed:
            self._reload_traefik()
        try:
            services_d.write_declaration_from_install(self.syrvis_home, updated)
        except Exception:  # noqa: BLE001 - best-effort; the re-pin stands
            pass

        compose_path = p["compose"]
        self._stop_service(name, compose_path)
        ok, msg = self._start_service(name, compose_path)
        if not ok:
            # The manifest/declaration now hold the new image but the container
            # is down — a state change history must show (a rollback target
            # picker needs to see the failed rollout).
            deployments.record_service_deploy(
                self.syrvis_home,
                updated,
                action="deploy",
                trigger="cli",
                outcome="failed",
                previous_image=old_image,
                detail=f"re-pinned to {new_image} but restart failed: {msg}",
            )
            return False, f"re-pinned to {new_image} but restart failed: {msg}"
        deployments.record_service_deploy(
            self.syrvis_home,
            updated,
            action="deploy",
            trigger="cli",
            outcome="success",
            previous_image=old_image,
        )
        self._fire_hooks(
            updated,
            "post-deploy",
            force=True,
            context={"image": new_image, "previous_image": old_image},
        )
        return True, f"Service '{name}' re-pinned: {old_image} -> {new_image}"

    def rollback(
        self, name: str, to: Optional[int] = None, *, trigger: str = "cli"
    ) -> Tuple[bool, str]:
        """Re-deploy a prior deployment revision of an installed L2 service.

        Helm-style: the target revision's manifest snapshot is restored through
        the FULL trust boundary (``ServiceDefinition.from_dict``) and redeployed
        via the proven set_image shape (pull first, swap manifest, dual-write
        the declaration, restart), then a NEW revision is recorded with
        ``rollback_of``. Data dir, env_file secrets, and materialized configs
        are untouched; current operator orchestration (enabled/critical) is
        preserved.

        GitOps-ephemeral: the next IaC apply/reconcile that still declares the
        newer image will redeploy it — make a rollback durable by reverting the
        image in the deployment repo too.
        """
        try:
            p = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        service_dir = p["service"]
        manifest_path = service_dir / "syrvis-service.yaml"
        if not manifest_path.exists():
            return False, f"Service '{name}' is not installed"
        if (service_dir / ".git").exists():
            return False, (
                f"'{name}' was installed from git — roll back via the repo "
                "(git revert) + 'syrvis service update'"
            )
        try:
            current = load_service_definition(service_dir)
        except Exception as e:  # noqa: BLE001
            return False, f"could not load manifest for '{name}': {e}"

        try:
            target = deployments.resolve_rollback_target(self.syrvis_home, name, to)
        except deployments.DeploymentError as e:
            return False, str(e)
        try:
            # Re-validate the stored snapshot through the trust boundary — a
            # tampered record cannot smuggle an unaudited key or a bad ref.
            restored = ServiceDefinition.from_dict(target["manifest"])
        except ServiceValidationError as e:
            return False, "revision {} manifest no longer validates: {}".format(
                target.get("revision"), e
            )
        # to_dict() drops source_url, but the tier:infra authorship gate needs
        # it — restore the recorded anchor (falling back to the current one).
        restored.source_url = target.get("source_url") or current.source_url

        def content(svc: ServiceDefinition) -> Dict[str, Any]:
            data = svc.to_dict()
            data.pop("enabled", None)
            data.pop("critical", None)
            return data

        if content(restored) == content(current):
            return True, "Service '{}' already matches revision {} — nothing to do".format(
                name, target.get("revision")
            )

        # design/26: a revision recorded at a DIFFERENT location must not be
        # re-materialized over an app that has data at its current home — the
        # compose would point at an empty (or stale) tree.
        if restored.location != current.location and self._dir_nonempty(
            self._current_data_root(name)
        ):
            return False, LOCATION_CHANGE_REFUSAL

        # The restored revision's hostname must not collide with a route some
        # OTHER service claimed since (same guard as install; last-writer-wins
        # in Traefik is a silent footgun).
        if restored.traefik.enabled and restored.traefik.subdomain:
            owner = self._subdomain_in_use(
                restored.traefik.subdomain,
                domain=restored.traefik.domain,
                exclude=name,
            )
            if owner:
                effective_host = "{}.{}".format(
                    restored.traefik.subdomain,
                    restored.traefik.domain or "<instance-domain>",
                )
                return False, (
                    "revision {} routes {!r}, which is now owned by service {!r} — "
                    "not rolling back".format(target.get("revision"), effective_host, owner)
                )

        old_image = current.image
        revision = target.get("revision")

        hooks_ok, runs = self._fire_hooks(
            restored,
            "pre-deploy",
            context={"image": restored.image, "previous_image": old_image},
        )
        if not hooks_ok:
            return False, "rollback aborted by pre-deploy hook: " + self._hook_failure_summary(runs)

        if restored.image != current.image:
            # Pull the TARGET image first: an unpullable (since-deleted) tag
            # must not tear down the running service.
            try:
                pull = subprocess.run(
                    ["docker", "pull", restored.image],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                return False, (f"pulling {restored.image} timed out — service left on {old_image}")
            except OSError as e:
                return False, f"could not run docker pull: {e}"
            if pull.returncode != 0:
                detail = (pull.stderr or pull.stdout or "").strip().splitlines()[-1:] or [""]
                deployments.record_service_deploy(
                    self.syrvis_home,
                    restored,
                    action="rollback",
                    trigger=trigger,
                    outcome="failed",
                    previous_image=old_image,
                    rollback_of=revision,
                    detail="target image not pullable ({})".format(detail[0]),
                )
                return False, (
                    f"could not pull {restored.image} ({detail[0]}) — "
                    f"service left unchanged on {old_image}"
                )

        self._write_manifest(restored, service_dir)
        self._generate_compose_file(restored)
        # write_config is change-aware and drops a stale route itself when the
        # restored revision is unrouted — one call covers both directions.
        try:
            domain = get_domain_from_env()
            _, traefik_changed = self.traefik_config.write_config(restored, domain)
        except ValueError as e:
            return False, f"Failed to update Traefik config: {e}"
        if traefik_changed:
            self._reload_traefik()
        try:
            # Restores CONTENT only; the operator's current enabled/critical is
            # preserved (a rollback never resurrects old orchestration).
            services_d.write_declaration_from_install(self.syrvis_home, restored)
        except Exception:  # noqa: BLE001 - best-effort; the rollback stands
            pass

        compose_path = p["compose"]
        self._stop_service(name, compose_path)
        ok, msg = self._start_service(name, compose_path)
        if not ok:
            deployments.record_service_deploy(
                self.syrvis_home,
                restored,
                action="rollback",
                trigger=trigger,
                outcome="failed",
                previous_image=old_image,
                rollback_of=revision,
                detail=f"rolled back to revision {revision} but restart failed: {msg}",
            )
            return False, f"rolled back to revision {revision} but restart failed: {msg}"
        deployments.record_service_deploy(
            self.syrvis_home,
            restored,
            action="rollback",
            trigger=trigger,
            outcome="success",
            previous_image=old_image,
            rollback_of=revision,
        )
        self._fire_hooks(
            restored,
            "post-deploy",
            force=True,
            context={"image": restored.image, "previous_image": old_image},
        )
        return True, "Service '{}' rolled back: {} -> {} (revision {})".format(
            name, old_image, restored.image, revision
        )

    # -------------------------------------------------------------------------
    # Declared one-shot tasks (operator-seam verb)
    # -------------------------------------------------------------------------

    _TASK_TIMEOUT_S = 300

    def run_task(self, name: str, task: str) -> Tuple[bool, str]:
        """Run a DECLARED one-shot task inside the service's running container.

        Tasks are pre-declared, schema-audited argvs in the installed manifest
        (``tasks: {name: {command: [...]}}``) — the encapsulated alternative to
        a break-glass ``docker exec`` (e.g. a DB bootstrap). The task runs in
        the service's OWN container under its existing confinement, so it
        grants no authority the container did not already have; the argv comes
        from the installed manifest only — never from the caller.
        """
        try:
            paths_ = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        manifest_path = paths_["service"] / "syrvis-service.yaml"
        if not manifest_path.exists():
            return False, f"Service '{name}' is not installed"
        try:
            service = load_service_definition(manifest_path)
        except Exception as e:  # noqa: BLE001 - unreadable/invalid manifest
            return False, f"could not load manifest for '{name}': {e}"
        if task not in service.tasks:
            declared = ", ".join(sorted(service.tasks)) or "(none)"
            return False, f"unknown task '{task}' for service '{name}' (declared: {declared})"

        ok, output = self._docker_exec_task(service, service.tasks[task], self._TASK_TIMEOUT_S)
        if not ok:
            return False, f"task '{task}' {output}"
        return True, output or f"task '{task}' completed"

    def _docker_exec_task(
        self, service: ServiceDefinition, task_argv: List[str], timeout: int
    ) -> Tuple[bool, str]:
        """Exec one audited argv inside the service's running container."""
        argv = ["docker", "exec", service.container_name] + list(task_argv)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        except OSError as e:
            return False, f"could not run docker: {e}"
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode != 0:
            return False, f"failed (exit {result.returncode}):\n{output}"
        return True, output

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

        # --- name re-validation + layout-aware root (design/26) ---------------
        # Legacy: the secrets root IS data/<name>. v2: home/secrets (slot
        # containment already asserted by _service_paths).
        try:
            paths_ = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        secrets_root_dir = paths_.get("secrets") or (self.data_dir / name)

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
        env_file_path = Path(os.path.realpath(str(secrets_root_dir / declared.env_file)))
        data_root = os.path.realpath(str(secrets_root_dir))
        if not str(env_file_path).startswith(data_root + os.sep):
            return False, (
                f"env_file {declared.env_file!r} escapes the service "
                f"{'secrets' if 'secrets' in paths_ else 'data'} directory (path traversal)"
            )

        # --- target dir must already exist (created by reconcile/install) -----
        if not secrets_root_dir.exists():
            return False, (
                f"data directory {secrets_root_dir} does not exist — "
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

    # -------------------------------------------------------------------------
    # Config management (operator-seam verb) — the jobs analog of write_secret
    # -------------------------------------------------------------------------

    def write_config(self, name: str, content: str) -> Tuple[bool, str]:
        """Write *content* to a declared job's conf file as root:root 0600.

        The exact jobs-side analog of :meth:`write_secret`: instead of a Layer 2
        service's ``data/<name>/<env_file>``, it renders a scheduled job's
        ``config/<name>.conf`` (e.g. login-alert.conf, immich-db-backup.conf,
        which carry an NTFY_URL). Same security contract, guard for guard:
        - Name is re-validated (defense in depth against unvalidated callers).
        - Name MUST be a DECLARED JOB in config/jobs.d/ (the authorization gate,
          analogous to secret_set's services.d gate) — this stops the operator
          writing confs for arbitrary/undeclared names.
        - Destination is realpath-containment-checked directly inside config/;
          ``<name>.conf`` is a single path component, so any traversal fails.
        - config/ must already exist (it does for any install); we FAIL rather
          than mkdir the home — and we never create a subdirectory.
        - Write is atomic: O_CREAT|O_EXCL temp in the same dir -> fsync ->
          os.replace -> chmod 0600. A dropped SSH connection or SIGKILL
          mid-stream never leaves a truncated .conf.
        - Content is capped at ``_SECRET_MAX_BYTES`` and must be non-empty.

        CALLER INVARIANT (security — the safety of this verb lives in the CONSUMER):
        the content is operator-controlled arbitrary data. A reader of <name>.conf may
        treat a value ONLY as inert data (a URL, a destination path it writes to, an
        integer) — NEVER as a command name, a path it later executes, or an
        ``eval``/``sh -c``/``source`` argument. Like ``secret set``, this verb is only as
        safe as what reads the file it writes.

        Returns (True, message) on success; (False, error) on any failure.
        Caller (CLI) must run as root (sudo) — ownership is root:root by default.
        """
        # --- input guards (defense in depth) --------------------------------
        if not content:
            return False, "config content must not be empty"
        content_bytes = content.encode("utf-8", errors="surrogateescape")
        if len(content_bytes) > self._SECRET_MAX_BYTES:
            return False, (
                f"config content too large ({len(content_bytes)} bytes; max {self._SECRET_MAX_BYTES})"
            )

        # --- name re-validation (same slug validator write_secret uses) ------
        # validate_service_name enforces the [a-z0-9][a-z0-9_-]{0,63} charset and
        # rejects '..'/traversal + reserved names. The declared-job gate below
        # independently re-validates the name against jobs.d's own name regex.
        try:
            validate_service_name(name, "job name")
        except ServiceValidationError as e:
            return False, str(e)

        # --- declaration check (name MUST be a declared JOB) -----------------
        try:
            declarations, _invalid = jobs_d.load_job_declarations(self.syrvis_home)
        except Exception as e:  # noqa: BLE001 - catch all load failures
            return False, f"could not load job declarations: {e}"

        if name not in declarations:
            return False, f"job {name!r} is not declared in config/jobs.d/"

        # --- path containment check ------------------------------------------
        config_dir = self.syrvis_home / "config"
        conf_path = Path(os.path.realpath(str(config_dir / f"{name}.conf")))
        config_root = os.path.realpath(str(config_dir))
        if not str(conf_path).startswith(config_root + os.sep):
            return False, (f"conf file for {name!r} escapes the config directory (path traversal)")

        # --- config dir must already exist (created by install) --------------
        # Do NOT mkdir the home — a missing config/ means a broken/absent install.
        if not config_dir.exists():
            return False, (
                f"config directory {config_dir} does not exist — "
                "this does not look like a SyrvisCore install"
            )

        # --- atomic 0600 write -----------------------------------------------
        dest = str(conf_path)
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
            return False, f"failed to write config: {e}"

        return True, f"wrote {dest}"

    # -------------------------------------------------------------------------
    # Deployment bundle (operator-seam verb) — one atomic install/update of a
    # single L2 service from a resolved syrvis-bundle (design/21).
    # -------------------------------------------------------------------------

    def deploy_bundle(self, bundle: "DeployBundle") -> Tuple[bool, str]:
        """Apply a resolved deployment bundle to ONE service, atomically.

        The encapsulated services-plane apply (design/21): manifest + non-secret
        configs + secret values → a running, up-to-date service in one call.
        Steps, in order — start LAST so the container reads a complete config on
        first boot (no crash-loop window):

          1. (re)write the services.d declaration (source of truth; outside the
             rollback boundary, so a failed apply keeps the intent for a retry).
          2. fresh service → install WITHOUT starting; existing → rewrite the
             effective manifest (data preserved).
          3. place each non-secret config into data/<name>/<dest> (0644).
          4. materialize the declared env_file (0600) from the bundle secrets.
          5. regenerate the compose + (re)start.

        Atomic PER CALL, with an ASYMMETRIC blast radius (know this):
          - FRESH install failure → drops everything it created (service + its
            just-made data dir); the declaration is kept (written by
            install_declaration, never a rollback target) so a retry works.
          - UPDATE failure → the service is taken DOWN (container stopped,
            services/<name>/ + compose removed) and the declaration + manifest now
            hold the NEW (failed) version; the OLD manifest/compose are NOT restored
            — this is deliberately NOT blue/green. data/ is preserved. A
            previously-healthy service is therefore torn down by a bad update;
            recovery is a re-deploy with a good bundle. Validate the bundle before
            applying (the CLI + schema already do; deploy-stack renders from vetted
            repo files).
        Secret VALUES are never logged or placed on argv.

        TARGETED: never touches another service (unlike reconcile, which
        converges/prunes the whole set). Caller (CLI) runs as root.
        """
        service = bundle.service
        name = service.name
        try:
            self._service_paths(name)  # re-validate name as a path component
        except ServiceValidationError as e:
            return False, str(e)

        self._ensure_directories()
        service_path = self.services_dir / name
        fresh = not service_path.exists()
        # Adoption probe (design/26): a v2 home that already has ANY content
        # (the end state of the app-move procedure — data, or even just a
        # pre-staged config/) predates this call; even a FRESH install's
        # failure rollback must never destroy it (adversarial review #6).
        adopted = False
        if service.location:
            probe_home = self._app_home(service)
            adopted = probe_home is not None and self._dir_nonempty(probe_home)

        # A deploy bundle is delivered over the operator seam → operator-authored by
        # construction (the bundle schema never carries source_url, so it's None
        # here). Mark it "deploy:<name>" so the tier:infra authorship gate (design/22)
        # accepts it on the FRESH path (install_declaration preserves a preset
        # source_url) AND so the assertion below covers the UPDATE path, which does
        # NOT route through _install_from_definition's gate. Without this, an update
        # could turn a benign on-disk service into an infra one without
        # re-authorization (adversarial review N1) — reachable only over the root
        # seam, but the gate must be explicit here, not incidental.
        if not service.source_url:
            service.source_url = "deploy:{}".format(name)
        if service.tier == "infra" and not str(service.source_url or "").startswith(
            OPERATOR_AUTHORED_PREFIXES
        ):
            return False, ("tier: infra is only permitted for an operator-authored declaration")

        hooks_ok, runs = self._fire_hooks(service, "pre-deploy", context={"image": service.image})
        if not hooks_ok:
            return False, "deploy aborted by pre-deploy hook: " + self._hook_failure_summary(runs)

        # 1. Declaration + install/manifest. The declaration is written in BOTH
        #    branches OUTSIDE the rollback boundary (a failed deploy keeps the
        #    declared intent for a retry — the install rollback never removes it).
        # Traefik reload is CONDITIONAL on a real route change: the fresh branch
        # handles its own (install_declaration reloads iff write_config reported
        # a change); the update branch sets this flag for the tail. The old
        # unconditional reload restarted the edge proxy once per deployed
        # service — a 12-service stack sweep meant 12 Traefik restarts.
        traefik_changed = False
        old_image: Optional[str] = None
        if fresh:
            # install_declaration writes the declaration + creates services/<name>/
            # + the manifest + installs WITHOUT starting (configs+secrets must land
            # before first boot). It rolls back its OWN artifacts on failure.
            # record/fire_hooks=False: the bundle records ONE deployment and
            # fires ONE pre/post-deploy pair at its own boundary.
            ok, msg = self.install_declaration(service, start=False, record=False, fire_hooks=False)
            if not ok:
                return False, msg
        else:
            # Update in place — rewrite declaration + effective manifest (data kept).
            # design/26 gates for THIS branch (the fresh branch routes through
            # _install_from_definition's): (a) location is immutable while the
            # app has data — checked against the INSTALLED manifest BEFORE it
            # is overwritten below; (b) a location must be operator-authored
            # (a bundle is, by construction — the check stays explicit, not
            # incidental, mirroring the tier N1 fix) and a mounted volume.
            try:
                refusal = self._location_change_refusal(name, service.location)
            except ServiceValidationError as e:
                return False, str(e)  # tampered installed manifest: fail closed
            if refusal:
                return False, refusal
            if service.location:
                if not str(service.source_url or "").startswith(OPERATOR_AUTHORED_PREFIXES):
                    return False, (
                        "location: is only permitted for an operator-authored declaration"
                    )
                if not LOCATION_RE.fullmatch(service.location):
                    return False, "invalid location {!r}: must match ^/volume<N>$".format(
                        service.location
                    )
                if not paths.is_mounted_volume(service.location):
                    return False, (
                        "location {!r} is not a mounted volume — refusing to "
                        "materialize the app home there".format(service.location)
                    )
            try:
                old_image = load_service_definition(service_path).image
            except Exception:  # noqa: BLE001 - unreadable prior manifest
                pass
            try:
                services_d.write_declaration(self.syrvis_home, service)
            except Exception as e:  # noqa: BLE001
                return False, f"could not write declaration for {name!r}: {e}"
            self._write_manifest(service, service_path)
            # Keep the route in step with the updated manifest — this path never
            # rewrote the dynamic config, so a changed traefik: block kept
            # serving the OLD route until the next fresh install.
            try:
                domain = get_domain_from_env()
                _, traefik_changed = self.traefik_config.write_config(service, domain)
            except ValueError as e:
                return False, f"Failed to configure Traefik: {e}"
            # Make the app home exist for the config/secret writes below —
            # exist_ok/adopting semantics (an update repairs missing slots, a
            # legacy->v2 transition over an EMPTY data root materializes them).
            home = self._app_home(service)
            if home is not None:
                try:
                    self._service_paths(name, service)  # slot containment first
                except ServiceValidationError as e:
                    return False, str(e)
                self._ensure_app_home(home)

        try:
            # 2. Configs — 0644 (container-readable) or 0600 (secret config file).
            for cfg in bundle.configs:
                ok, msg = self._place_config(name, cfg.dest, cfg.content, secret=cfg.secret)
                if not ok:
                    raise RuntimeError(msg)

            # 3. Secrets → the declared env_file (0600). Reuse write_secret (atomic,
            #    dest derived from env_file). msg carries no secret value.
            if bundle.secrets:
                env_body = "".join(f"{k}={v}\n" for k, v in bundle.secrets.items())
                ok, msg = self.write_secret(name, env_body)
                if not ok:
                    raise RuntimeError(msg)

            # 4. Compose + start LAST.
            compose_path = self._generate_compose_file(service)
            ok, msg = self._start_service(name, compose_path)
            if not ok:
                raise RuntimeError(f"failed to start: {msg}")
            # `docker compose up -d` compares the COMPOSE SPEC (image/command/
            # volumes/env), NOT the CONTENT of bind-mounted files — so on an UPDATE
            # a changed config/secret would keep serving the OLD content (the
            # process read it once at start). Restart the container to re-read it.
            # Only when there IS a config/secret and the compose didn't already
            # recreate it (fresh installs start with the final content; config-less
            # services like victoria-metrics need no restart).
            if not fresh and (bundle.configs or bundle.secrets):
                self._compose(name, compose_path, "restart", timeout=90)
            if traefik_changed:
                self._reload_traefik()
        except Exception as e:  # noqa: BLE001
            # Fresh: drop everything (incl. the just-created data dir) — unless
            # a pre-populated v2 home was ADOPTED (it predates this call).
            # Update: keep data + declaration for a clean retry.
            self._rollback_add(name, keep_data=(not fresh) or adopted)
            # Either branch left changed state (fresh keeps the declared intent;
            # update tore the service down on the new manifest) — history must
            # show the failed rollout.
            deployments.record_service_deploy(
                self.syrvis_home,
                service,
                action="deploy",
                trigger="deploy",
                outcome="failed",
                previous_image=old_image,
                detail=f"deploy of {name!r} failed ({e})",
                configs=bundle.configs,
            )
            return False, f"deploy of {name!r} failed ({e})"

        verb = "installed" if fresh else "updated"
        deployments.record_service_deploy(
            self.syrvis_home,
            service,
            action="deploy",
            trigger="deploy",
            outcome="success",
            previous_image=old_image,
            configs=bundle.configs,
        )
        self._fire_hooks(
            service,
            "post-deploy",
            force=True,
            context={"image": service.image, "previous_image": old_image or ""},
        )
        return True, (
            f"deployed {name} ({verb}; {len(bundle.configs)} config(s), "
            f"{len(bundle.secrets)} secret(s))"
        )

    def _place_config(
        self, name: str, relpath: str, content: str, secret: bool = False
    ) -> Tuple[bool, str]:
        """Write a bundle config into data/<name>/<relpath> (root-owned).

        Mode is 0600 when ``secret`` (a config FILE carrying a secret — e.g. an
        ntfy scfg / a Grafana datasource token), else 0644 (a config a non-root
        container reads over a :ro mount). Same atomic contract as
        :meth:`write_secret`. The dest was schema-validated (relative, no '..',
        not the env_file); we realpath-confine it to data/<name>/ here (defense in
        depth). The 0600-downgrade guard is 0644-ONLY — a secret write is itself
        0600, so overwriting an existing 0600 there is not a downgrade.
        """
        mode = 0o600 if secret else 0o644
        if not isinstance(content, str):
            return False, "config content must be a string"
        content_bytes = content.encode("utf-8", errors="surrogateescape")
        if len(content_bytes) > self._SECRET_MAX_BYTES:
            return False, (
                f"config content too large ({len(content_bytes)} bytes; max {self._SECRET_MAX_BYTES})"
            )
        if os.path.isabs(relpath) or ".." in PurePosixPath(relpath).parts:
            return False, f"invalid config dest {relpath!r}: must be relative with no '..'"

        # Layout-aware root (design/26): legacy configs land under data/<name>
        # (today's behavior, dests conventionally carry a config/ prefix); a v2
        # app's configs land under home/config (dests are config-root-relative).
        try:
            paths_ = self._service_paths(name)
        except ServiceValidationError as e:
            return False, str(e)
        data_dir_for_svc = paths_.get("config") or (self.data_dir / name)
        dest_path = Path(os.path.realpath(str(data_dir_for_svc / relpath)))
        data_root = os.path.realpath(str(data_dir_for_svc))
        if dest_path != Path(data_root) and not str(dest_path).startswith(data_root + os.sep):
            return False, f"config dest {relpath!r} escapes the service config directory"
        if not data_dir_for_svc.exists():
            return False, f"config directory {data_dir_for_svc} does not exist"
        # Never DOWNGRADE an existing 0600 secret to a world-readable 0644 write.
        # A secret config writes 0600 (not a downgrade), so this guard is 0644-only.
        if not secret and dest_path.exists():
            try:
                existing_mode = stat.S_IMODE(dest_path.stat().st_mode)
                if not (existing_mode & 0o077):
                    return False, (
                        f"refusing to overwrite {dest_path} (mode {existing_mode:04o}; a "
                        "0600-style secret) with a non-secret 0644 config"
                    )
            except OSError:
                pass

        dest = str(dest_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + f".syrvis.{os.getpid()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, dest)
            os.chmod(dest, mode)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False, f"failed to write config: {e}"
        return True, f"wrote {dest}"
