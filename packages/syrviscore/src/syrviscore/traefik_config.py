"""
Traefik configuration generation for SyrvisCore.

Generates static and dynamic configuration files for Traefik reverse proxy.
Also handles Layer 2 service dynamic configurations.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .service_schema import ServiceDefinition


# =============================================================================
# Synology Services Configuration
# =============================================================================
# Define Synology NAS services that can be proxied through Traefik.
# Each service has a subdomain, target port, and protocol.
#
# To add a new service:
#   1. Add an entry to SYNOLOGY_SERVICES
#   2. Set the corresponding env var to enable it (e.g., SYNOLOGY_DSM_ENABLED=true)
#   3. Run 'syrvis setup' to regenerate configs

# This dict is THE catalog of Synology passthrough services — the single source
# consumed by the compose/traefik generators, hostnames.build_report(), the
# validators' endpoint checks, and the dashboard's links/routes endpoints. Do
# not redefine service→subdomain mappings elsewhere.
SYNOLOGY_SERVICES = {
    # Service key: (subdomain, port, protocol, label, description)
    # Note: DS File removed - use SMB directly to NAS IP instead
    "photos": {
        "subdomain": "photos",
        "port": 5001,
        "protocol": "https",
        "label": "Photos",
        "description": "Photos (Synology Photos / Photo Station)",
        "env_enabled": "SYNOLOGY_PHOTOS_ENABLED",
    },
    "dsm": {
        "subdomain": "dsm",
        "port": 5001,
        "protocol": "https",
        "label": "DSM",
        "description": "DSM Web Interface",
        "env_enabled": "SYNOLOGY_DSM_ENABLED",
    },
    "drive": {
        "subdomain": "drive",
        "port": 6690,
        "protocol": "https",
        "label": "Drive",
        "description": "Synology Drive",
        "env_enabled": "SYNOLOGY_DRIVE_ENABLED",
    },
    "audio": {
        "subdomain": "audio",
        "port": 5001,
        "protocol": "https",
        "label": "Audio",
        "description": "Audio Station",
        "env_enabled": "SYNOLOGY_AUDIO_ENABLED",
    },
    "video": {
        "subdomain": "video",
        "port": 5001,
        "protocol": "https",
        "label": "Video",
        "description": "Video Station",
        "env_enabled": "SYNOLOGY_VIDEO_ENABLED",
    },
    "webdav": {
        "subdomain": "files",
        "port": 5006,
        "protocol": "https",
        "label": "WebDAV",
        "description": "WebDAV (Synology WebDAV Server — file access, e.g. iOS Files)",
        "env_enabled": "SYNOLOGY_WEBDAV_ENABLED",
    },
}

# The primordial core UIs Traefik always routes, at fixed subdomains — the
# companion catalog to SYNOLOGY_SERVICES, consumed by hostnames.py and the
# dashboard's links endpoint (mirrors the compose labels + dynamic config).
PRIMORDIAL_UIS = (
    {
        "service": "portainer",
        "subdomain": "portainer",
        "label": "Portainer",
        "description": "Container management",
    },
    {
        "service": "traefik",
        "subdomain": "traefik",
        "label": "Traefik",
        "description": "Reverse proxy dashboard",
    },
)


def get_enabled_synology_services() -> Dict[str, dict]:
    """Get list of enabled Synology services from environment variables."""
    enabled = {}
    for key, config in SYNOLOGY_SERVICES.items():
        env_var = config["env_enabled"]
        if os.getenv(env_var, "").lower() in ("true", "1", "yes"):
            enabled[key] = config
    return enabled


def generate_traefik_static_config() -> str:
    """
    Generate Traefik static configuration (traefik.yml).

    Static configuration defines:
    - API/Dashboard settings
    - Entry points (ports 80, 443)
    - Provider (file-based only — ONE routing mechanism for every tier: core
      services, Synology passthroughs, and Layer 2 services are all routed by
      generated files under /config, and Traefik never needs the docker socket)
    - Logging configuration
    - Let's Encrypt certificate resolver

    Returns:
        YAML content as string
    """
    acme_email = os.getenv("ACME_EMAIL", "admin@example.com")

    # DNS-01 is required for names that resolve to a private IP (split-horizon /
    # internal-only): it proves control via a DNS TXT record, so certs issue +
    # renew without the domain being publicly reachable. It kicks in when a DNS
    # token is configured (Cloudflare is the default provider) or when forced;
    # otherwise HTTP-01. The provider and its resolvers are configurable so a
    # non-Cloudflare lego provider (route53, deSEC, …) works without editing code
    # — set TRAEFIK_ACME_DNS_PROVIDER + forward that provider's credential env
    # vars via TRAEFIK_ACME_DNS_ENV (see compose._generate_traefik_service).
    if (
        os.getenv("CLOUDFLARE_DNS_API_TOKEN")
        or os.getenv("TRAEFIK_ACME_DNS_ENV")
        or os.getenv("TRAEFIK_ACME_CHALLENGE", "").lower() == "dns"
    ):
        provider = os.getenv("TRAEFIK_ACME_DNS_PROVIDER", "cloudflare").strip() or "cloudflare"
        resolvers = [
            r.strip()
            for r in os.getenv("TRAEFIK_ACME_DNS_RESOLVERS", "1.1.1.1:53,1.0.0.1:53").split(",")
            if r.strip()
        ]
        resolver_lines = "\n".join('          - "{}"'.format(r) for r in resolvers)
        acme_challenge = (
            "      dnsChallenge:\n"
            "        provider: {}\n".format(provider) + "        resolvers:\n" + resolver_lines
        )
    else:
        acme_challenge = """      httpChallenge:
        entryPoint: web"""

    # ACME CA server: default is Let's Encrypt production (Traefik's own default
    # when caServer is omitted). Set TRAEFIK_ACME_CA_SERVER to the LE STAGING
    # endpoint while testing (avoids the strict prod rate limits), or to another
    # ACME CA (ZeroSSL, an internal step-ca, …).
    ca_server = os.getenv("TRAEFIK_ACME_CA_SERVER", "").strip()
    ca_line = "\n      caServer: {}".format(ca_server) if ca_server else ""

    config = f"""# Traefik Static Configuration
# Generated by SyrvisCore

api:
  dashboard: true
  insecure: true  # Dashboard on :8080 (internal only)

# Liveness endpoint served on the API entrypoint (:8080) so health checks pass.
ping: {{}}

entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"

providers:
  file:
    directory: "/config"
    watch: true

log:
  level: INFO
  filePath: "/logs/traefik.log"

accessLog:
  filePath: "/logs/access.log"

certificatesResolvers:
  letsencrypt:
    acme:
      email: {acme_email}
      storage: /acme.json{ca_line}
{acme_challenge}
"""
    return config


def generate_synology_routers_config(domain: str, backend_ip: str) -> str:
    """
    Generate Traefik router configuration for enabled Synology services.

    Args:
        domain: Base domain (e.g., example.com)
        backend_ip: Accepted for symmetry with the services generator; routers are
            host-based (``Host(subdomain.domain)``) so the backend IP is unused here.

    Returns:
        YAML snippet for routers section
    """
    enabled_services = get_enabled_synology_services()
    if not enabled_services:
        return ""

    lines = [
        "",
        "    # =========================================================================",
        "    # Synology NAS Services",
        "    # =========================================================================",
    ]

    for key, config in enabled_services.items():
        subdomain = config["subdomain"]
        lines.extend(
            [
                "",
                f"    # {config['description']} (HTTP -> HTTPS redirect)",
                f"    synology-{key}:",
                f'      rule: "Host(`{subdomain}.{domain}`)"',
                f"      service: synology-{key}",
                "      entryPoints:",
                "        - web",
                "      middlewares:",
                "        - https-redirect",
                "",
                f"    # {config['description']} (HTTPS with Let's Encrypt)",
                f"    synology-{key}-secure:",
                f'      rule: "Host(`{subdomain}.{domain}`)"',
                f"      service: synology-{key}",
                "      entryPoints:",
                "        - websecure",
                "      tls:",
                "        certResolver: letsencrypt",
            ]
        )

    return "\n".join(lines)


def generate_synology_services_config(backend_ip: str) -> str:
    """
    Generate Traefik service configuration for enabled Synology services.

    Args:
        backend_ip: The address Traefik dials to reach DSM services. Under macvlan
            this is the shim IP (SHIM_IP), not NAS_IP — the Traefik container
            cannot reach the host at NAS_IP directly, but a packet arriving at the
            host on the shim interface IS accepted *because DSM system services
            (DSM/Photos/Drive on 5000/5001/6690) bind 0.0.0.0*. If DSM were ever
            pinned to a specific NAS_IP, this routing would need a host route to
            NAS_IP over the shim instead.

    Returns:
        YAML snippet for services section
    """
    enabled_services = get_enabled_synology_services()
    if not enabled_services:
        return ""

    # Entries only (no ``services:`` header) — the dynamic config always opens
    # the services section for the core containers and appends these after.
    lines = [
        "",
        "    # =========================================================================",
        "    # Synology NAS Services",
        "    # =========================================================================",
    ]

    for key, config in enabled_services.items():
        protocol = config["protocol"]
        port = config["port"]
        lines.extend(
            [
                f"    synology-{key}:",
                "      loadBalancer:",
                "        servers:",
                f'          - url: "{protocol}://{backend_ip}:{port}"',
                "        serversTransport: insecure-skip-verify@file",
            ]
        )

    # Add serversTransport for self-signed certs on Synology
    lines.extend(
        [
            "",
            "  serversTransports:",
            "    insecure-skip-verify:",
            "      insecureSkipVerify: true",
        ]
    )

    return "\n".join(lines)


def _host_router_pair(name: str, host: str, service: str) -> str:
    """The standard HTTP-redirect + HTTPS/TLS router pair for one hostname."""
    return f"""
    # {name} (HTTP -> HTTPS redirect)
    {name}-http:
      rule: "Host(`{host}`)"
      service: {service}
      entryPoints:
        - web
      middlewares:
        - https-redirect

    # {name} (HTTPS with Let's Encrypt)
    {name}:
      rule: "Host(`{host}`)"
      service: {service}
      entryPoints:
        - websecure
      tls:
        certResolver: letsencrypt
"""


def _core_service_routes(domain: str) -> Tuple[str, str]:
    """Router + service snippets for the core containers (file provider).

    Core services are file-routed exactly like Layer 2 services — ONE routing
    mechanism, and Traefik needs no docker provider/socket. Portainer is
    primordial (always routed); the SyrvisCore dashboard is routed when the
    stack enables it. Backends are container-DNS names on the proxy network,
    the same pattern ServiceTraefikConfig uses.
    """
    from . import stack as stack_mod

    # Load the stack ONCE and let a genuinely-corrupt stack.yaml raise (StackError):
    # a routing regen that silently dropped the dashboard router here would restart
    # Traefik and de-route a running dashboard with no error surfaced. load_stack
    # already falls back to the env-inferred default when the file is merely absent.
    stack = stack_mod.load_stack()
    dashboard_enabled = stack.is_enabled("dashboard")
    dash_subdomain = stack.setting("dashboard", "subdomain") or os.getenv(
        "DASHBOARD_SUBDOMAIN", "dash"
    )

    # Portainer's subdomain comes from the PRIMORDIAL_UIS catalog (the single
    # source hostnames.py + the dashboard also read) so the routed host can't
    # drift from the host the deployment's DNS reconciler is told to create.
    portainer_sub = next(ui["subdomain"] for ui in PRIMORDIAL_UIS if ui["service"] == "portainer")
    routers = _host_router_pair("portainer", f"{portainer_sub}.{domain}", "portainer")
    services = """    portainer:
      loadBalancer:
        servers:
          - url: "http://portainer:9000"
"""
    if dashboard_enabled:
        # Prefixed `syrvis-dashboard` so it can't collide with the `dashboard`
        # router (Traefik's own UI) above.
        routers += _host_router_pair(
            "syrvis-dashboard", f"{dash_subdomain}.{domain}", "syrvis-dashboard"
        )
        services += """    syrvis-dashboard:
      loadBalancer:
        servers:
          - url: "http://syrviscore-dashboard:8000"
"""
    return routers, services


def generate_traefik_dynamic_config() -> str:
    """
    Generate Traefik dynamic configuration (config/dynamic.yml).

    Dynamic configuration can be updated without restarting Traefik.
    Defines routing rules, services, and middleware — for Traefik's own UI,
    the core containers (portainer, the SyrvisCore dashboard), and the
    Synology passthrough services. Layer 2 services get their own per-service
    files in /config/dynamic/ (ServiceTraefikConfig).

    Returns:
        YAML content as string
    """
    domain = os.getenv("DOMAIN", "example.com")
    nas_ip = os.getenv("NAS_IP", "")
    shim_ip = os.getenv("SHIM_IP", "")

    # For macvlan: Traefik container cannot reach NAS directly at NAS_IP.
    # It must use SHIM_IP (the macvlan shim interface) to reach the host.
    # If SHIM_IP is set, use it as the backend; otherwise fall back to NAS_IP.
    backend_ip = shim_ip if shim_ip else nas_ip

    # Generate Synology services config if backend IP is available
    synology_routers = ""
    synology_services = ""
    if backend_ip:
        synology_routers = generate_synology_routers_config(domain, backend_ip)
        synology_services = generate_synology_services_config(backend_ip)

    core_routers, core_services = _core_service_routes(domain)

    config = f"""# Traefik Dynamic Configuration
# Generated by SyrvisCore
# This file can be updated without restarting Traefik

http:
  routers:
    # =========================================================================
    # SyrvisCore Services
    # =========================================================================

    # Traefik Dashboard (HTTP -> HTTPS redirect)
    dashboard:
      rule: "Host(`traefik.{domain}`)"
      service: api@internal
      entryPoints:
        - web
      middlewares:
        - https-redirect

    # Traefik Dashboard (HTTPS with Let's Encrypt)
    dashboard-secure:
      rule: "Host(`traefik.{domain}`)"
      service: api@internal
      entryPoints:
        - websecure
      tls:
        certResolver: letsencrypt
{core_routers}{synology_routers}

  middlewares:
    # Redirect HTTP to HTTPS
    https-redirect:
      redirectScheme:
        scheme: https
        permanent: true

  services:
{core_services}{synology_services}
"""
    return config


# =============================================================================
# Layer 2 Service Configuration
# =============================================================================


class ServiceTraefikConfig:
    """Generate Traefik dynamic configuration files for Layer 2 services.

    Each service gets its own config file in $SYRVIS_HOME/data/traefik/config/dynamic/
    that Traefik hot-reloads automatically via the file provider.

    The config files are placed in the Traefik config mount which maps to:
    - Host: $SYRVIS_HOME/data/traefik/config/
    - Container: /config/

    So files in $SYRVIS_HOME/data/traefik/config/dynamic/ appear as /config/dynamic/
    in the container, and Traefik watches the entire /config/ directory.
    """

    def __init__(self, config_dir: Optional[Path] = None):
        """Initialize the generator.

        Args:
            config_dir: Path to Traefik dynamic config directory.
                       Defaults to $SYRVIS_HOME/data/traefik/config/dynamic/
        """
        if config_dir:
            self.config_dir = config_dir
        else:
            syrvis_home = os.environ.get("SYRVIS_HOME", "")
            if not syrvis_home:
                raise ValueError("SYRVIS_HOME environment variable not set")
            # Use data/traefik/config/dynamic/ to match the volume mount
            self.config_dir = Path(syrvis_home) / "data" / "traefik" / "config" / "dynamic"

    def ensure_directory(self) -> None:
        """Ensure the dynamic config directory exists."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def generate_config(self, service: "ServiceDefinition", domain: str) -> Dict[str, Any]:
        """Generate Traefik configuration for a service.

        Args:
            service: Service definition
            domain: Instance base domain (e.g., "example.com").  When the service
                declaration includes a ``traefik.domain`` override, the effective
                hostname uses that domain instead.  Both the HTTP-redirect router
                and the HTTPS/TLS router use the effective hostname.

                DNS-01 note: wildcard certs are issued per-zone by the Cloudflare
                DNS-01 provider (CF_DNS_API_TOKEN).  A domain override only works
                correctly when the target zone (e.g. example.org) is also within
                the scope of that token — scope CF_DNS_API_TOKEN to every zone
                the instance routes.

        Returns:
            Traefik configuration dictionary
        """
        if not service.traefik.enabled or not service.traefik.subdomain:
            return {}

        # NOTE: ``service.traefik.exposure`` is intentionally NOT consumed here.
        # Exposure ('internal' vs 'tunnel') is declared intent reported by
        # ``syrvis stack hostnames``; SyrvisCore routes both identically at the
        # Traefik layer (same router + letsencrypt resolver). See exposure.py.

        # Use the per-service domain override when set; fall back to the instance domain.
        effective_domain = service.traefik.domain if service.traefik.domain else domain
        host = f"{service.traefik.subdomain}.{effective_domain}"
        name = service.name

        # Build router configurations
        config: Dict[str, Any] = {
            "http": {
                "routers": {
                    # HTTP router (redirects to HTTPS)
                    f"{name}-http": {
                        "entryPoints": ["web"],
                        "rule": f"Host(`{host}`)",
                        "middlewares": ["https-redirect"],
                        "service": name,
                    },
                    # HTTPS router
                    name: {
                        "entryPoints": ["websecure"],
                        "rule": f"Host(`{host}`)",
                        "tls": {
                            "certResolver": "letsencrypt",
                        },
                        "service": name,
                    },
                },
                "services": {
                    name: {
                        "loadBalancer": {
                            "servers": [
                                {"url": f"http://{service.container_name}:{service.traefik.port}"}
                            ]
                        }
                    }
                },
            }
        }

        # Add custom middlewares if specified
        if service.traefik.middlewares:
            config["http"]["routers"][name]["middlewares"] = service.traefik.middlewares

        return config

    def write_config(self, service: "ServiceDefinition", domain: str) -> Optional[Path]:
        """Write Traefik configuration file for a service.

        Args:
            service: Service definition
            domain: Base domain

        Returns:
            Path to written config file, or None if no config needed
        """
        self.ensure_directory()

        config = self.generate_config(service, domain)
        if not config:
            # No Traefik config needed for this service
            return None

        config_path = self.config_dir / f"{service.name}.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return config_path

    def remove_config(self, service_name: str) -> bool:
        """Remove Traefik configuration file for a service.

        Args:
            service_name: Name of the service

        Returns:
            True if config was removed, False if it didn't exist
        """
        config_path = self.config_dir / f"{service_name}.yaml"
        if config_path.exists():
            config_path.unlink()
            return True
        return False

    def list_configs(self) -> List[str]:
        """List all service configs in the dynamic directory.

        Returns:
            List of service names with configs
        """
        if not self.config_dir.exists():
            return []

        return [f.stem for f in self.config_dir.glob("*.yaml")]


def get_domain_from_env() -> str:
    """Get the domain from environment variables.

    Returns:
        Domain string

    Raises:
        ValueError: If DOMAIN not set
    """
    domain = os.environ.get("DOMAIN", "")
    if not domain:
        # Try loading from .env file
        syrvis_home = os.environ.get("SYRVIS_HOME", "")
        if syrvis_home:
            env_file = Path(syrvis_home) / "config" / ".env"
            if env_file.exists():
                try:
                    with open(env_file, "r") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("DOMAIN="):
                                domain = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
                except OSError:
                    # The unprivileged operator can't read the 0600 .env; DOMAIN
                    # is only needed for an optional URL. Fall through to the
                    # ValueError below (the documented contract callers handle).
                    pass

    if not domain:
        raise ValueError("DOMAIN environment variable not set")

    return domain
