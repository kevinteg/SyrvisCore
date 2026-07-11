"""Dashboard runtime settings, sourced from environment variables.

Field names are the lowercase of their env var (pydantic-settings matches
case-insensitively), e.g. ``dashboard_auth_mode`` ← ``DASHBOARD_AUTH_MODE``.
All are optional with safe defaults so a bare LAN / open-source install boots
without Cloudflare Access, OIDC, or DDNS configured.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["cloudflare", "oidc", "both", "none"]


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # --- server ---
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    dashboard_static_dir: str = ""  # where the built SPA lives (set in the image)

    # --- SyrvisCore install (read by syrviscore.paths via the SYRVIS_HOME env) ---
    syrvis_home: str = ""

    # --- health probing ---
    aggregator_ttl_s: float = 5.0
    probe_timeout_s: float = 3.0
    traefik_url: str = "http://traefik:8080"
    portainer_url: str = "http://portainer:9000"
    cloudflared_url: str = "http://cloudflared:20241"
    public_ip_url: str = "https://api.ipify.org"  # DDNS: detect the home public IP
    cloudflare_api_url: str = "https://api.cloudflare.com/client/v4"  # DDNS: compare records

    # --- management ---
    enable_l2_mutations: bool = False
    ssh_target: str = "nas"  # host alias used when rendering `ssh <target> '...'` hints

    # --- auth ---
    dashboard_auth_mode: AuthMode = "none"
    dashboard_session_secret: str = ""  # signs the local session cookie (generated if empty)
    # Cloudflare Access
    cloudflare_access_team: str = ""  # <team> in https://<team>.cloudflareaccess.com
    cloudflare_access_aud: str = ""  # the Access application AUD tag
    # Local OIDC (Synology SSO Server by default; any OIDC IdP works)
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_url: str = ""  # e.g. https://dash.<domain>/auth/callback

    @property
    def auth_mode(self) -> AuthMode:
        return self.dashboard_auth_mode

    @property
    def cloudflare_enabled(self) -> bool:
        return self.dashboard_auth_mode in ("cloudflare", "both")

    @property
    def oidc_enabled(self) -> bool:
        return self.dashboard_auth_mode in ("oidc", "both")


@lru_cache
def get_settings() -> DashboardSettings:
    """Process-wide settings (cached). Tests override via app state, not this."""
    return DashboardSettings()
