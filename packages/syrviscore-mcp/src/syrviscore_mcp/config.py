"""
Configuration for the SyrvisCore MCP server.

Loaded from a TOML file (default ~/.config/syrviscore-mcp/config.toml, override
via SYRVISCORE_MCP_CONFIG) plus a few environment overrides. No secrets are
inlined: the SSH key is referenced from a dedicated ssh_config, and the HMAC
token secret is read from an env var. The loader validates aggressively — an
unsafe or half-configured server refuses to start rather than doing something
surprising against a critical NAS.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .errors import ConfigError

DEFAULT_CONFIG_PATH = "~/.config/syrviscore-mcp/config.toml"

# Users that must never be the SSH operator: over-privileged / human accounts
# (using one would conflate human and MCP access). This is the generic floor; a
# deployment adds its own human admin accounts via safety.forbidden_ssh_users.
_FORBIDDEN_SSH_USERS = {"root", "admin"}


@dataclass
class NASConfig:
    # [nas]
    host: str
    ssh_target: str
    ssh_config_file: Path
    control_path: str
    command_timeout_s: int = 120

    # [layout]
    profile: str = "prod"
    syrvisctl_path: str = ""
    syrvis_wrapper: str = ""
    syrvis_home: str = ""

    # [privilege]
    use_sudo: bool = True
    sudo_binary: str = "sudo"

    # [safety]
    managed_marker: str = "syrviscore"
    environment: str = "production"
    git_url_allowed_hosts: List[str] = field(default_factory=list)
    # Registries an image-first `service_run` may pull from. Fails CLOSED like
    # git_url_allowed_hosts: empty means service_run is disabled, never "any".
    image_allowed_registries: List[str] = field(default_factory=list)
    # Deployment-specific human accounts that must never be the SSH operator
    # (added to the generic _FORBIDDEN_SSH_USERS floor).
    forbidden_ssh_users: List[str] = field(default_factory=list)

    # [tokens]
    token_secret_env: str = "SYRVISCORE_MCP_TOKEN_SECRET"
    token_secret_file: str = ""
    token_ttl_s: int = 300

    # resolved
    ssh_user: Optional[str] = None

    def is_production(self) -> bool:
        # Accept BOTH the 'prod' shorthand and the 'production' long form so a
        # natural typo can't silently downgrade security to an ephemeral secret.
        # (environment is validated against a known set at load time.)
        return self.environment in ("prod", "production")

    def token_secret(self) -> bytes:
        secret = os.environ.get(self.token_secret_env)
        if secret:
            return secret.encode()
        # Opt-in fallback: a dedicated 0600 secret file (tokens.secret_file) so the
        # server can launch from a .mcp.json without the operator exporting an env
        # var, and without a secret ever living in a repo. ToolContext's per-process
        # salt still voids outstanding tokens on restart regardless of this value.
        if self.token_secret_file:
            p = Path(os.path.expanduser(self.token_secret_file))
            if p.is_file():
                data = p.read_text().strip()
                if data:
                    return data.encode()
        if self.is_production():
            raise ConfigError(
                f"{self.token_secret_env} is not set",
                operator_hint=(
                    f"export {self.token_secret_env}=<random>, or set tokens.secret_file to "
                    "a 0600 file holding the secret, before starting the server"
                ),
            )
        # Non-production: a per-process ephemeral secret is fine (voids on restart)
        return os.urandom(32)


def _abs(path: str, what: str) -> str:
    if not path or not str(path).startswith("/"):
        raise ConfigError(f"{what} must be an absolute path (got {path!r})")
    return path


def _parse_ssh_user(ssh_config_file: Path, ssh_target: str) -> Optional[str]:
    """Extract the User for ssh_target from the ssh_config file (best-effort)."""
    if not ssh_config_file.exists():
        return None
    current = None
    user = None
    for raw in ssh_config_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, val = parts[0].lower(), parts[1].strip()
        if key == "host":
            current = val
        elif key == "user" and current == ssh_target:
            user = val
    return user


def load_config(path: Optional[str] = None) -> NASConfig:
    """Load and validate the server configuration.

    Raises:
        ConfigError: on any missing/unsafe setting.
    """
    cfg_path = Path(
        os.path.expanduser(path or os.environ.get("SYRVISCORE_MCP_CONFIG", DEFAULT_CONFIG_PATH))
    )
    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path}",
            operator_hint="create ~/.config/syrviscore-mcp/config.toml (see the MCP README)",
        )

    data = tomllib.loads(cfg_path.read_text())
    nas = data.get("nas", {})
    layout = data.get("layout", {})
    priv = data.get("privilege", {})
    safety = data.get("safety", {})
    tokens = data.get("tokens", {})

    ssh_config_file = Path(
        os.path.expanduser(nas.get("ssh_config_file", "~/.config/syrviscore-mcp/ssh_config"))
    )

    profile = layout.get("profile", "prod")
    if profile not in ("dev", "prod"):
        raise ConfigError(f"layout.profile must be 'dev' or 'prod' (got {profile!r})")

    cfg = NASConfig(
        host=os.environ.get("SYRVISCORE_NAS_HOST", nas.get("host", "")),
        ssh_target=nas.get("ssh_target", ""),
        ssh_config_file=ssh_config_file,
        control_path=os.path.expanduser(
            nas.get("control_path", "~/.config/syrviscore-mcp/cm-%r@%h:%p")
        ),
        command_timeout_s=int(nas.get("command_timeout_s", 120)),
        profile=profile,
        syrvisctl_path=_abs(layout.get("syrvisctl_path", ""), "layout.syrvisctl_path"),
        syrvis_wrapper=_abs(layout.get("syrvis_wrapper", ""), "layout.syrvis_wrapper"),
        syrvis_home=_abs(layout.get("syrvis_home", ""), "layout.syrvis_home"),
        use_sudo=bool(priv.get("use_sudo", True)),
        sudo_binary=priv.get("sudo_binary", "sudo"),
        managed_marker=safety.get("managed_marker", "syrviscore"),
        environment=safety.get("environment", "production"),
        git_url_allowed_hosts=list(safety.get("git_url_allowed_hosts", [])),
        image_allowed_registries=list(safety.get("image_allowed_registries", [])),
        forbidden_ssh_users=list(safety.get("forbidden_ssh_users", [])),
        token_secret_env=tokens.get("secret_env", "SYRVISCORE_MCP_TOKEN_SECRET"),
        token_secret_file=os.path.expanduser(tokens.get("secret_file", "")),
        token_ttl_s=int(tokens.get("ttl_s", 300)),
    )

    if not cfg.host:
        raise ConfigError("nas.host is required (or set SYRVISCORE_NAS_HOST)")
    if not cfg.ssh_target:
        raise ConfigError("nas.ssh_target is required")

    cfg.ssh_user = _parse_ssh_user(ssh_config_file, cfg.ssh_target)
    forbidden = _FORBIDDEN_SSH_USERS | {u.lower() for u in cfg.forbidden_ssh_users}
    if cfg.ssh_user and cfg.ssh_user.lower() in forbidden:
        raise ConfigError(
            f"ssh User for {cfg.ssh_target!r} is {cfg.ssh_user!r} — use a dedicated "
            f"operator account, not {sorted(forbidden)}",
        )

    # Validate the environment label so a typo can't slip through unnoticed.
    if cfg.environment not in ("prod", "production", "dev", "test", "development"):
        raise ConfigError(
            f"safety.environment {cfg.environment!r} is not a recognized value",
            operator_hint="use one of: prod, production, dev, test",
        )

    # service_add clones + runs new code; require an explicit host allowlist in
    # production so it fails closed rather than accepting any git host.
    if cfg.is_production() and not cfg.git_url_allowed_hosts:
        raise ConfigError(
            "safety.git_url_allowed_hosts must be non-empty in production",
            operator_hint="set safety.git_url_allowed_hosts (e.g. ['github.com'])",
        )

    return cfg
