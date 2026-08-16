"""
Docker Compose configuration generator for SyrvisCore.

Generates docker-compose.yaml with the core-tier services: Traefik, Portainer,
Cloudflared, and the SyrvisCore dashboard.

Image versions come from, in order of precedence:
1. an explicit ``config_path`` handed to :class:`ComposeGenerator`;
2. the active version's bundled ``build/config.yaml`` (a release can attach a
   ``config.yaml`` asset, which ``syrvisctl install`` copies into the version
   tree — the channel for shipping image bumps without a code change);
3. the built-in :data:`DEFAULT_DOCKER_IMAGES` pins below (the committed source
   of truth in this repo).

Network settings come from ``.env``.
"""

import ipaddress
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# Default Docker image versions - used when config.yaml doesn't exist.
# full_image is DIGEST-PINNED (repo:tag@sha256:…): the tag stays for readability,
# the digest makes the pull immutable — a re-pushed tag can't silently change the
# core image. Digests resolved 2026-07-25; bump the tag AND re-resolve the digest
# together (Renovate pinDigests does both). `syrvis images` marks a tag-only core
# pin as needs-attention; a digest pin from a trusted publisher reads as trusted.
DEFAULT_DOCKER_IMAGES = {
    # v3.7 line, NOT v3.6: Traefik ends v3.6 SECURITY support on 2026-08-16, while
    # v3.7 (GA 2026-05-05) keeps active + security support. v3.7.10 and v3.6.25 both
    # shipped 2026-07-31 from the same security batch and BOTH carry zero open
    # advisories; the v3.6.5 we were on carried 17 (10 high), incl. CVE-2026-71324
    # (unauthenticated CONNECT response poisoning on the shared keep-alive pool) and
    # CVE-2026-27141 (HTTP/2 frame panic = unauthenticated remote DoS).
    # Migration surface here is empty: file provider only (no docker socket, no k8s),
    # one middleware (https-redirect), exact-Host routers only, no tls.options, no
    # http3 — so every v3.7-only behaviour change (wildcard Host / HostSNI matching,
    # bare `*` catch-all, TLSOptions on wildcard domains) has nothing to bind to, and
    # the non-k8s ones that DO apply (StripPrefix 400, underscoreHeadersStrategy,
    # CONNECT 501) are in v3.6.25 too. NOTE both hops cross v3.6.7, which flipped
    # entryPoints.*.http.encodedCharacters.allow* to default TRUE — encoded
    # slash/percent/etc. now reach backends instead of 400ing.
    # Digest resolved 2026-08-14 from the multi-arch index (contains linux/amd64).
    "traefik": {
        "image": "traefik",
        "tag": "v3.7.10",
        "full_image": "traefik:v3.7.10@sha256:9c3b91d5fb7770853ca5c1124a23c34bf2d9b47ffaebeab2614cbaf410dcb2ac",
        "description": "",
    },
    "portainer": {
        "image": "portainer/portainer-ce",
        "tag": "2.44.0-alpine",
        "full_image": "portainer/portainer-ce:2.44.0-alpine@sha256:5376fd96f0bae14be7285ceb24c5cf9470dc23f19cdde74ff4c65d11cbe96eb2",
        "description": "",
    },
    "cloudflared": {
        "image": "cloudflare/cloudflared",
        "tag": "2026.7.3",
        "full_image": "cloudflare/cloudflared:2026.7.3@sha256:e39ee8da81ad5e05d77f38d2f51c60ca51bf2a8450ac3abab50c17fdb91d91bf",
        "description": "",
    },
    # The dashboard image is versioned INDEPENDENTLY of the service (owner decision
    # 2026-07-31): its tag advances only on a real dashboard change, NOT every
    # service release. This pin tag MUST equal the dashboard package __version__
    # (running == pinned; asserted by test_compose + release-service.sh). A
    # service-only release leaves this pin untouched, so `apply-instance --converge`
    # recreates the dashboard container only when the dashboard actually changed.
    # (Whether CI rebuilds the image on a service-only release — the baked
    # syrviscore-lib freshness tradeoff — is a separate, still-open decision.)
    "dashboard": {
        "image": "ghcr.io/kevinteg/syrviscore-dashboard",
        "tag": "0.5.8",
        # 0.5.8 and not 0.5.2: ghcr already holds 0.5.2-0.5.7 — 0.5.2-0.5.6 are
        # relics of the pre-2026-07-31 force-sync era when every service release
        # pushed a dashboard image, and 0.5.7 is a real published build — so a
        # dashboard bump lands on the first free tag. The invariant (test_compose
        # TestImagePinLockstep) is pin == the DASHBOARD package __version__,
        # which advances only on a real dashboard change.
        # Digest resolved 2026-08-12 from the CI-published 0.5.8 index (contains
        # linux/amd64 — the NAS architecture — plus its attestation manifest).
        "full_image": "ghcr.io/kevinteg/syrviscore-dashboard:0.5.8@sha256:9893e5c56c4808ef32b3e41e20f4f253461d21509c728bc61dcdda836f36301a",
        "description": "SyrvisCore web dashboard",
    },
}

# The cloudflared metrics/`/ready` listener (`TUNNEL_METRICS`, the env-var form of
# the `--metrics` flag — no argv surgery, so the `tunnel run` command stays intact).
#
# WHY 0.0.0.0 IS SAFE HERE: cloudflared's own default is a localhost-only listener
# on the first free port in 20241-20245, which is reachable from inside its own
# netns and nowhere else — useless to a sibling container, and non-deterministic to
# boot. Binding 0.0.0.0 widens it to this container's networks, which is exactly one
# network: the internal `proxy` bridge. The port is NEVER published (no `ports:` key
# on the cloudflared service — see _generate_cloudflared_service), so it is not bound
# on the NAS host and not reachable from the LAN or the internet; only containers
# attached to `proxy` can dial it, by container name. That is the same posture
# traefik's :8080 api/internal entrypoint already runs in.
#
# WHO CONSUMES IT (two out-of-band readers, which is why the port is a contract and
# not an implementation detail):
#   1. the SyrvisCore dashboard's tunnel probe — GET /ready, so the UI can report
#      REAL edge connectivity rather than "container up" (see dashboard.md). Its
#      `CLOUDFLARED_URL` is rendered from this same value, so the two cannot drift.
#   2. a deployment repo's scraper — home-tech's vmagent scrapes /metrics over
#      `proxy` (job_name: cloudflared) to arm the svc-edge board's tunnel row
#      (cloudflared_tunnel_ha_connections / _total_requests / _request_errors).
#      ha_connections == 0 while the container is UP is the tunnel failure that a
#      container-lifecycle alert structurally cannot see.
# Overridable per instance with the `metrics_port` setting on cloudflared in
# config/stack.yaml (declared config, not product code) — a deployment repo whose
# scrape config already targets another port declares it there instead of forking
# this pin. Both consumers above follow the declared value.
CLOUDFLARED_METRICS_PORT = 20241


class ComposeGenerator:
    """Generate docker-compose.yaml from build configuration and environment variables."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the compose generator.

        Args:
            config_path: Explicit path to a build configuration file. None (the
                default) resolves the active version's bundled
                ``build/config.yaml`` when one exists, falling back to the
                built-in :data:`DEFAULT_DOCKER_IMAGES` pins.
        """
        self.config_path: Optional[Path] = Path(config_path) if config_path else None
        self.build_config: Optional[Dict[str, Any]] = None

    @staticmethod
    def _resolve_default_config_path() -> Optional[Path]:
        """The active version's bundled config.yaml, or None when absent.

        Best-effort: an unresolvable SYRVIS_HOME (unit tests, fresh box) simply
        means the built-in pins apply.
        """
        try:
            from . import paths

            bundled = paths.get_version_config_yaml()
            return bundled if bundled.exists() else None
        except Exception:  # noqa: BLE001 - no install context -> built-in pins
            return None

    def load_config(self) -> Dict[str, Any]:
        """
        Load the build configuration (image versions).

        Precedence: explicit config_path > the active version's bundled
        config.yaml > the built-in DEFAULT_DOCKER_IMAGES pins.

        Returns:
            Parsed configuration dictionary
        """
        if self.config_path is None:
            self.config_path = self._resolve_default_config_path()

        if self.config_path is not None and self.config_path.exists():
            with open(self.config_path, "r") as f:
                self.build_config = yaml.safe_load(f)

            if not self.build_config or "docker_images" not in self.build_config:
                raise ValueError("Invalid config: missing docker_images section")
        else:
            # Built-in pinned versions (the committed source of truth)
            self.build_config = {
                "metadata": {
                    "description": "Using built-in pinned Docker image versions",
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

    @staticmethod
    def _traefik_acme_env() -> list:
        """The Traefik container's env: TZ + the ACME DNS-01 credential(s).

        Cloudflare is the default (lego reads ``CF_DNS_API_TOKEN``, fed from
        ``CLOUDFLARE_DNS_API_TOKEN``). For any OTHER lego provider, list its
        credential env var names in ``TRAEFIK_ACME_DNS_ENV`` (comma-separated)
        and they are forwarded from ``.env`` into the container — so a
        non-Cloudflare provider works without editing code. Values are never
        inlined here; each is a ``${VAR:-}`` compose interpolation.
        """
        env = [
            "TZ=${TZ:-UTC}",
            # Cloudflare DNS-01 token (the default provider). Harmless when unset.
            "CF_DNS_API_TOKEN=${CLOUDFLARE_DNS_API_TOKEN:-}",
        ]
        extra = os.getenv("TRAEFIK_ACME_DNS_ENV", "")
        seen = {"CF_DNS_API_TOKEN"}
        for name in (n.strip() for n in extra.split(",")):
            # Only forward real env var names; never re-add the CF default.
            if name and name.replace("_", "").isalnum() and name not in seen:
                env.append("{0}=${{{0}:-}}".format(name))
                seen.add(name)
        return env

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
            # Must exceed the static config's lifeCycle.graceTimeOut (20s) or
            # every stop ends in panic("Timeout while stopping traefik") instead
            # of a clean drain + exit 0.
            "stop_grace_period": "30s",
            "security_opt": ["no-new-privileges:true"],
            "networks": {
                "syrvis-macvlan": {
                    "ipv4_address": traefik_ip,
                },
                "proxy": {},
            },
            # No port bindings needed - traefik has its own IP via macvlan
            "environment": self._traefik_acme_env(),
            # No docker socket: routing is file-provider only (core, Synology
            # passthrough, and L2 routes are all generated files under /config),
            # so Traefik holds no host-level authority at all.
            "volumes": [
                "../data/traefik/traefik.yml:/traefik.yml:ro",
                "../data/traefik/config/:/config/:ro",
                "../data/traefik/acme.json:/acme.json",
                "../data/traefik/logs:/logs",
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
            # Routed by the file provider (traefik_config._core_service_routes),
            # like every other tier — no traefik labels.
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                "../data/portainer:/data",
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

    def _cloudflared_metrics_port(self) -> int:
        """The declared cloudflared metrics port (stack setting, else the default pin).

        A bad value (non-integer, out of range) falls back to
        :data:`CLOUDFLARED_METRICS_PORT` rather than rendering a compose file that
        cloudflared would refuse to start on — a typo in stack.yaml must not take
        the tunnel down, and the default is always a working listener.
        """
        stack = getattr(self, "_stack", None)
        raw = stack.setting("cloudflared", "metrics_port", None) if stack else None
        if raw is None:
            return CLOUDFLARED_METRICS_PORT
        try:
            port = int(raw)
        except (TypeError, ValueError):
            return CLOUDFLARED_METRICS_PORT
        return port if 1 <= port <= 65535 else CLOUDFLARED_METRICS_PORT

    def _generate_cloudflared_service(self) -> Optional[Dict[str, Any]]:
        """Generate Cloudflared service configuration on bridge network."""
        if "cloudflared" not in self.build_config["docker_images"]:
            return None

        image = self.build_config["docker_images"]["cloudflared"]["full_image"]

        return {
            "image": image,
            "container_name": "cloudflared",
            "restart": "unless-stopped",
            # No `ports:` — the metrics listener below stays inside `proxy`.
            "networks": ["proxy"],
            "environment": [
                "TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}",
                # Metrics/`/ready` on the proxy network for the dashboard's tunnel
                # probe and a deployment repo's scraper — see CLOUDFLARED_METRICS_PORT
                # for why 0.0.0.0 is safe and who reads it.
                "TUNNEL_METRICS=0.0.0.0:{}".format(self._cloudflared_metrics_port()),
            ],
            "command": "tunnel --no-autoupdate run",
        }

    def _generate_dashboard_service(self) -> Optional[Dict[str, Any]]:
        """Generate the SyrvisCore dashboard service (web observability + management).

        Emitted whenever a ``dashboard`` image is configured. Runs on the ``proxy``
        network so it can reach traefik:8080 / portainer:9000 / cloudflared's metrics
        port (:20241 by default — see :data:`CLOUDFLARED_METRICS_PORT`),
        holds the docker socket for container-safe management, and mounts the
        config/data/manifest so the in-process ``syrviscore`` library resolves
        ``SYRVIS_HOME``.
        """
        if "dashboard" not in self.build_config["docker_images"]:
            return None

        image = self.build_config["docker_images"]["dashboard"]["full_image"]
        stack = getattr(self, "_stack", None)

        # Read-only by default (safe to expose, no management). Opt into container
        # control by declaring `management: true` on the dashboard in stack.yaml —
        # only do that once auth is wired (rw socket = host-level authority).
        management = bool(stack.setting("dashboard", "management", False)) if stack else False
        socket_mount = "/var/run/docker.sock:/var/run/docker.sock" + ("" if management else ":ro")
        data_mount = "../data:/syrvis/data" + ("" if management else ":ro")
        # Layer 2 service definitions live here; the dashboard reads them via
        # ServiceManager.list(). Without this mount the dashboard shows no L2
        # services at all. Read-only unless management (add/remove) is declared.
        services_mount = "../services:/syrvis/services" + ("" if management else ":ro")

        return {
            "image": image,
            "container_name": "syrviscore-dashboard",
            "restart": "unless-stopped",
            "security_opt": ["no-new-privileges:true"],
            "networks": ["proxy"],
            "environment": [
                "SYRVIS_HOME=/syrvis",
                # The tunnel probe target, rendered from the SAME declared port as
                # cloudflared's TUNNEL_METRICS listener — pinned explicitly rather
                # than left to the image's compiled-in default so that changing
                # `metrics_port` moves the listener and the probe together (a
                # silently-drifted probe would report the tunnel down while it is
                # healthy). Harmless when cloudflared is not enabled.
                "CLOUDFLARED_URL=http://cloudflared:{}".format(self._cloudflared_metrics_port()),
                "DASHBOARD_AUTH_MODE=${DASHBOARD_AUTH_MODE:-none}",
                "DASHBOARD_SESSION_SECRET=${DASHBOARD_SESSION_SECRET:-}",
                "ENABLE_L2_MUTATIONS=${ENABLE_L2_MUTATIONS:-false}",
                # SSH_TARGET is resolved to the NAS IP at setup time (explicit
                # SSH_TARGET > NAS_IP > 'nas'); NAS_IP is passed too so the
                # dashboard can resolve privileged-action hints inline even when
                # an older .env still carries the placeholder alias.
                "SSH_TARGET=${SSH_TARGET:-nas}",
                "NAS_IP=${NAS_IP:-}",
                "CLOUDFLARE_ACCESS_TEAM=${CLOUDFLARE_ACCESS_TEAM:-}",
                "CLOUDFLARE_ACCESS_AUD=${CLOUDFLARE_ACCESS_AUD:-}",
                "OIDC_ISSUER=${OIDC_ISSUER:-}",
                "OIDC_CLIENT_ID=${OIDC_CLIENT_ID:-}",
                "OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET:-}",
                "OIDC_REDIRECT_URL=${OIDC_REDIRECT_URL:-}",
            ],
            "volumes": [
                # Socket is :ro unless management is declared (rw = container control).
                socket_mount,
                "../config:/syrvis/config:ro",
                data_mount,
                services_mount,
                # so paths.get_syrvis_home() trusts SYRVIS_HOME (it looks for the manifest).
                "../.syrviscore-manifest.json:/syrvis/.syrviscore-manifest.json:ro",
            ],
            # Routed by the file provider (traefik_config._core_service_routes,
            # router prefix `syrvis-dashboard`) — no traefik labels.
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

        # The operator (docker group) must READ this for service_list/verify; a
        # root write otherwise lands root:root 0600 and locks it out. Best-effort
        # chgrp docker + 0640 (the compose carries no secrets — those live in
        # .env, which stays 0600). verify --fix (config_tree_perms) self-heals if
        # this is skipped on a non-root/edge write.
        try:
            import grp as _grp

            gid = _grp.getgrnam("docker").gr_gid
            os.chown(str(output_file), -1, gid)
            output_file.chmod(0o640)
        except (KeyError, OSError):
            pass

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
    config_path: Optional[str] = None, output_path: str = "docker-compose.yaml"
) -> Dict[str, Any]:
    """
    Helper function to generate docker-compose.yaml from build config.

    Args:
        config_path: Explicit build-config path; None resolves the active
            version's bundled config.yaml, else the built-in pins.
        output_path: Path where to save the compose file

    Returns:
        Generated compose configuration dictionary
    """
    generator = ComposeGenerator(config_path)
    return generator.generate_and_save(output_path=output_path)
