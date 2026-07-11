"""
Service definition schema for Layer 2 services.

Each service is defined by a syrvis-service.yaml file in its git repository.
This module provides the dataclasses for parsing and validating these definitions.

SECURITY: this schema is the trust boundary for third-party repositories.
A syrvis-service.yaml is attacker-controlled input that ends up as filesystem
paths (services/<name>, data/<name>, compose/<name>.yaml, a Traefik-watched
config file) and as a docker-compose file that root starts. Every field is
therefore strictly validated here — names are constrained to a safe charset,
host mounts are restricted to the service's own data directory, and unknown
keys are rejected outright.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import yaml

from syrviscore.errors import SyrvisError

from . import exposure as exposure_mod

# Safe identifier: what we allow as a service/container/network name.
# Used directly as a path component and a compose project name.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Single DNS label for Traefik subdomains
SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names owned by the core stack — a third-party service may not impersonate
# or replace them.
RESERVED_NAMES = frozenset({"traefik", "portainer", "cloudflared", "proxy", "syrvis-macvlan"})

ALLOWED_RESTART = frozenset({"no", "always", "on-failure", "unless-stopped"})

# The complete set of keys a syrvis-service.yaml may contain. Anything else
# is rejected — this is what stops a manifest from smuggling compose options
# we never audited (privileged, cap_add, devices, network_mode, ...).
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "version",
        "image",
        "description",
        "author",
        "homepage",
        "container_name",
        "traefik",
        "environment",
        "env_file",
        "volumes",
        "networks",
        "depends_on",
        "config_templates",
        "restart",
        "healthcheck",
        "resources",
    }
)

# healthcheck sub-schema (audited subset of compose's healthcheck)
ALLOWED_HEALTHCHECK_KEYS = frozenset({"test", "interval", "timeout", "retries", "start_period"})
DURATION_RE = re.compile(r"^\d+(s|m|h)$")

# resources sub-schema (service-level compose limits)
ALLOWED_RESOURCE_KEYS = frozenset({"cpus", "memory"})
CPUS_RE = re.compile(r"^\d+(\.\d+)?$")
MEMORY_RE = re.compile(r"^\d+(b|k|m|g)$", re.IGNORECASE)


class ServiceValidationError(SyrvisError, ValueError):
    """A syrvis-service.yaml failed validation (unsafe or malformed).

    Also a ValueError so existing ``except ValueError`` call sites keep catching it.
    """

    code = "service_invalid"


def validate_service_name(name: str, what: str = "name") -> str:
    """Validate a service/container/network identifier.

    The value is used as a filesystem path component and compose project
    name, so the charset is deliberately narrow.
    """
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ServiceValidationError(
            "Invalid {} {!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}".format(what, name)
        )
    if name in RESERVED_NAMES:
        raise ServiceValidationError(
            "Invalid {} {!r}: reserved for the SyrvisCore core stack".format(what, name)
        )
    return name


def _validate_relative_subpath(value: str, what: str) -> str:
    """Validate a path that must stay inside its designated directory."""
    if not isinstance(value, str) or not value:
        raise ServiceValidationError("{} must be a non-empty string".format(what))
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise ServiceValidationError(
            "Invalid {} {!r}: absolute paths and '..' are not allowed".format(what, value)
        )
    return value


def _validate_image(image: str) -> str:
    """Validate an image reference: pinned tag or digest, never :latest."""
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ServiceValidationError("Invalid image reference {!r}".format(image))
    ref = image
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        if not re.match(r"^sha256:[a-f0-9]{64}$", digest):
            raise ServiceValidationError("Invalid image digest in {!r}".format(image))
        return image
    # Tag is everything after the last ':' unless that segment contains '/'
    # (which would make it a registry port, not a tag)
    tag = None
    if ":" in ref:
        candidate = ref.rsplit(":", 1)[1]
        if "/" not in candidate:
            tag = candidate
    if not tag:
        raise ServiceValidationError(
            "Image {!r} has no tag: pin a specific version (house rule: no floating images)".format(
                image
            )
        )
    if tag == "latest":
        raise ServiceValidationError(
            "Image {!r} uses :latest — pin a specific version tag".format(image)
        )
    return image


def _validate_healthcheck(data: Any) -> Dict[str, Any]:
    """Validate an audited subset of compose's healthcheck.

    Allowed: test (a list whose first element is CMD or CMD-SHELL), interval,
    timeout, start_period (``\\d+(s|m|h)``), retries (1-10). Anything else is
    rejected — same allowlist philosophy as the top-level keys.
    """
    if not isinstance(data, dict):
        raise ServiceValidationError("healthcheck must be a mapping")
    unknown = set(data.keys()) - ALLOWED_HEALTHCHECK_KEYS
    if unknown:
        raise ServiceValidationError(
            "healthcheck: unknown keys {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_HEALTHCHECK_KEYS))
            )
        )

    test = data.get("test")
    if (
        not isinstance(test, list)
        or not test
        or test[0] not in ("CMD", "CMD-SHELL")
        or not all(isinstance(part, str) for part in test)
    ):
        raise ServiceValidationError(
            "healthcheck.test must be a list starting with CMD or CMD-SHELL"
        )

    for key in ("interval", "timeout", "start_period"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not DURATION_RE.match(value):
                raise ServiceValidationError(
                    "healthcheck.{} must match <number>(s|m|h), got {!r}".format(key, value)
                )

    if "retries" in data:
        retries = data["retries"]
        if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 10:
            raise ServiceValidationError("healthcheck.retries must be an integer 1-10")

    return dict(data)


def _validate_resources(data: Any) -> Dict[str, str]:
    """Validate resource limits: cpus (decimal) and/or memory (<n>(b|k|m|g))."""
    if not isinstance(data, dict):
        raise ServiceValidationError("resources must be a mapping")
    unknown = set(data.keys()) - ALLOWED_RESOURCE_KEYS
    if unknown:
        raise ServiceValidationError(
            "resources: unknown keys {} (allowed: cpus, memory)".format(", ".join(sorted(unknown)))
        )
    out: Dict[str, str] = {}
    if "cpus" in data:
        cpus = str(data["cpus"])
        if not CPUS_RE.match(cpus):
            raise ServiceValidationError("resources.cpus must be a decimal, e.g. '1.5'")
        out["cpus"] = cpus
    if "memory" in data:
        memory = str(data["memory"])
        if not MEMORY_RE.match(memory):
            raise ServiceValidationError("resources.memory must be <number>(b|k|m|g), e.g. '512m'")
        out["memory"] = memory
    if not out:
        raise ServiceValidationError("resources must declare cpus and/or memory")
    return out


def _validate_volume(vol: str) -> str:
    """Validate a volume entry against the mount policy.

    Allowed:
      - named volumes: ``myvolume:/container/path[:mode]``
      - relative host paths (resolved under data/<service>/):
        ``subdir:/container/path[:mode]``
    Refused:
      - absolute host paths (no /etc, no /, no /var/run/docker.sock)
      - '..' traversal, '$' expansions, docker.sock in any form
      - modes other than ro/rw
    """
    if not isinstance(vol, str) or not vol:
        raise ServiceValidationError("Volume entries must be non-empty strings")
    if "docker.sock" in vol:
        raise ServiceValidationError(
            "Volume {!r}: mounting the Docker socket is not permitted".format(vol)
        )
    if "$" in vol:
        raise ServiceValidationError(
            "Volume {!r}: environment expansion is not permitted".format(vol)
        )

    parts = vol.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ServiceValidationError(
            "Volume {!r}: expected 'source:/container/path[:mode]'".format(vol)
        )

    host, container = parts[0], parts[1]
    mode = parts[2] if len(parts) == 3 else "rw"

    if mode not in ("ro", "rw"):
        raise ServiceValidationError("Volume {!r}: mode must be ro or rw".format(vol))

    if not PurePosixPath(container).is_absolute() or ".." in PurePosixPath(container).parts:
        raise ServiceValidationError(
            "Volume {!r}: container path must be absolute (no '..')".format(vol)
        )

    if host.startswith("/") or host.startswith("~"):
        raise ServiceValidationError(
            "Volume {!r}: absolute host paths are not permitted; use a path "
            "relative to the service data directory or a named volume".format(vol)
        )
    if ".." in PurePosixPath(host).parts:
        raise ServiceValidationError("Volume {!r}: '..' is not permitted".format(vol))

    return vol


@dataclass
class TraefikConfig:
    """Traefik routing configuration for a service."""

    enabled: bool = True
    subdomain: str = ""
    port: int = 80
    middlewares: List[str] = field(default_factory=list)
    # How the routed service is reached from outside: "internal" (LAN-only) or
    # "tunnel" (Cloudflare Tunnel + Access). Declared intent only — SyrvisCore
    # routes both the same; it drives the `syrvis stack hostnames` report.
    exposure: str = exposure_mod.DEFAULT

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TraefikConfig":
        """Create TraefikConfig from dictionary."""
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=data.get("enabled", True),
            subdomain=data.get("subdomain", ""),
            port=data.get("port", 80),
            middlewares=data.get("middlewares", []),
            exposure=str(data.get("exposure") or exposure_mod.DEFAULT).strip().lower(),
        )


@dataclass
class ConfigTemplate:
    """Template file to copy during service installation."""

    source: str
    dest: str

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "ConfigTemplate":
        """Create ConfigTemplate from dictionary."""
        return cls(
            source=data.get("source", ""),
            dest=data.get("dest", ""),
        )


@dataclass
class ServiceDefinition:
    """Complete service definition from syrvis-service.yaml."""

    name: str
    version: str
    image: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    container_name: str = ""
    traefik: TraefikConfig = field(default_factory=TraefikConfig)
    environment: List[str] = field(default_factory=list)
    # A data-dir-relative env file (installed 0600) — the recommended home for
    # secrets, keeping them out of this manifest.
    env_file: str = ""
    volumes: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    config_templates: List[ConfigTemplate] = field(default_factory=list)
    restart: str = "unless-stopped"
    healthcheck: Optional[Dict[str, Any]] = None
    resources: Optional[Dict[str, str]] = None
    # Source information (set after loading)
    source_path: Optional[Path] = None
    source_url: Optional[str] = None

    def __post_init__(self):
        """Set defaults after initialization."""
        if not self.container_name:
            self.container_name = self.name
        if not self.networks:
            self.networks = ["proxy"]
        elif "proxy" not in self.networks:
            self.networks.append("proxy")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceDefinition":
        """Create ServiceDefinition from dictionary (strictly validated)."""
        if not isinstance(data, dict):
            raise ServiceValidationError("Service definition must be a mapping")

        unknown = set(data.keys()) - ALLOWED_TOP_LEVEL_KEYS
        if unknown:
            raise ServiceValidationError(
                "Unknown keys in service definition: {} — only audited keys are "
                "accepted".format(", ".join(sorted(unknown)))
            )

        # Validate required fields
        required = ["name", "version", "image"]
        missing = [f for f in required if f not in data]
        if missing:
            raise ServiceValidationError(f"Missing required fields: {', '.join(missing)}")

        name = validate_service_name(data["name"], "service name")
        container_name = data.get("container_name", name)
        validate_service_name(container_name, "container_name")
        image = _validate_image(data["image"])

        restart = data.get("restart", "unless-stopped")
        if restart not in ALLOWED_RESTART:
            raise ServiceValidationError(
                "Invalid restart policy {!r}: allowed: {}".format(
                    restart, ", ".join(sorted(ALLOWED_RESTART))
                )
            )

        environment = data.get("environment", [])
        if not isinstance(environment, list):
            raise ServiceValidationError("environment must be a list of KEY=VALUE strings")
        for entry in environment:
            if not isinstance(entry, str) or "=" not in entry:
                raise ServiceValidationError(
                    "Invalid environment entry {!r}: expected KEY=VALUE".format(entry)
                )
            key = entry.split("=", 1)[0]
            if not ENV_KEY_RE.match(key):
                raise ServiceValidationError("Invalid environment variable name {!r}".format(key))

        volumes = data.get("volumes", [])
        if not isinstance(volumes, list):
            raise ServiceValidationError("volumes must be a list")
        for vol in volumes:
            _validate_volume(vol)

        networks = data.get("networks", [])
        if not isinstance(networks, list):
            raise ServiceValidationError("networks must be a list")
        for net in networks:
            if net == "proxy":
                continue
            validate_service_name(net, "network name")

        depends_on = data.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ServiceValidationError("depends_on must be a list")
        if depends_on:
            # Each Layer-2 service runs as its OWN single-service compose project
            # (-p syrvis-<name>), so compose `depends_on` — which only orders
            # services WITHIN one project — can never reference another Syrvis
            # service. Reject it clearly instead of writing a silent no-op that
            # fails at docker-run time. (Sidecars need a real multi-container
            # manifest, which the schema does not yet support.)
            raise ServiceValidationError(
                "depends_on is not supported: each service is its own compose "
                "project, so it cannot depend on another Syrvis service. Remove "
                "the depends_on block (multi-container manifests are not yet supported)."
            )
        for dep in depends_on:
            validate_service_name(dep, "depends_on entry")

        templates = []
        for t in data.get("config_templates", []):
            template = ConfigTemplate.from_dict(t)
            _validate_relative_subpath(template.source, "config template source")
            _validate_relative_subpath(template.dest, "config template dest")
            templates.append(template)

        env_file = data.get("env_file", "")
        if env_file:
            _validate_relative_subpath(env_file, "env_file")

        healthcheck = None
        if data.get("healthcheck") is not None:
            healthcheck = _validate_healthcheck(data["healthcheck"])

        resources = None
        if data.get("resources") is not None:
            resources = _validate_resources(data["resources"])

        traefik = TraefikConfig.from_dict(data.get("traefik"))
        if traefik.enabled:
            if not SUBDOMAIN_RE.match(traefik.subdomain or ""):
                raise ServiceValidationError(
                    "Invalid traefik subdomain {!r}: must be a single DNS label".format(
                        traefik.subdomain
                    )
                )
            if not isinstance(traefik.port, int) or not 1 <= traefik.port <= 65535:
                raise ServiceValidationError(
                    "Invalid traefik port {!r}: must be 1-65535".format(traefik.port)
                )
            if not exposure_mod.is_valid(traefik.exposure):
                raise ServiceValidationError(
                    "Invalid traefik exposure {!r}: must be one of {}".format(
                        traefik.exposure, ", ".join(exposure_mod.EXPOSURES)
                    )
                )

        return cls(
            name=name,
            version=str(data["version"]),
            image=image,
            description=data.get("description", ""),
            author=data.get("author", ""),
            homepage=data.get("homepage", ""),
            container_name=container_name,
            traefik=traefik,
            environment=environment,
            env_file=env_file,
            volumes=volumes,
            networks=networks,
            depends_on=depends_on,
            config_templates=templates,
            restart=restart,
            healthcheck=healthcheck,
            resources=resources,
        )

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "ServiceDefinition":
        """Load service definition from YAML file."""
        if not yaml_path.exists():
            raise FileNotFoundError(f"Service definition not found: {yaml_path}")

        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty service definition: {yaml_path}")

        service = cls.from_dict(data)
        service.source_path = yaml_path.parent
        return service

    def to_dict(self) -> Dict[str, Any]:
        """Convert service definition to dictionary."""
        result = {
            "name": self.name,
            "version": self.version,
            "image": self.image,
            "container_name": self.container_name,
            "restart": self.restart,
        }

        if self.description:
            result["description"] = self.description
        if self.author:
            result["author"] = self.author
        if self.homepage:
            result["homepage"] = self.homepage

        if self.traefik.enabled:
            result["traefik"] = {
                "enabled": self.traefik.enabled,
                "subdomain": self.traefik.subdomain,
                "port": self.traefik.port,
                "exposure": self.traefik.exposure,
            }
            if self.traefik.middlewares:
                result["traefik"]["middlewares"] = self.traefik.middlewares

        if self.environment:
            result["environment"] = self.environment
        if self.env_file:
            result["env_file"] = self.env_file
        if self.volumes:
            result["volumes"] = self.volumes
        if self.networks:
            result["networks"] = self.networks
        if self.depends_on:
            result["depends_on"] = self.depends_on
        if self.config_templates:
            result["config_templates"] = [
                {"source": t.source, "dest": t.dest} for t in self.config_templates
            ]
        if self.healthcheck:
            result["healthcheck"] = self.healthcheck
        if self.resources:
            result["resources"] = self.resources

        return result


def load_service_definition(path: Path) -> ServiceDefinition:
    """Load a service definition from a directory or YAML file.

    Args:
        path: Path to service directory or syrvis-service.yaml file

    Returns:
        Parsed ServiceDefinition

    Raises:
        FileNotFoundError: If service definition not found
        ValueError: If service definition is invalid
    """
    if path.is_dir():
        yaml_path = path / "syrvis-service.yaml"
    else:
        yaml_path = path

    return ServiceDefinition.from_yaml(yaml_path)
