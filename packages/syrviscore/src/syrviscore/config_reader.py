"""Structured, redaction-aware reader for the runtime SyrvisCore config.

This is the single source of truth for "read the ``.env`` and hand back a safe,
structured view of it". Both ``syrvis config show`` (the CLI) and the dashboard
web adapter import ``read_config`` so they apply *one* secret-masking rule and
*one* component-detection rule.

It reads the **runtime** ``.env`` at ``$SYRVIS_HOME/config/.env`` — the schema
``setup.py`` actually writes (``DOMAIN``, ``ACME_EMAIL``, ``CLOUDFLARE_TUNNEL_TOKEN``,
``NETWORK_*`` …) — never the stale repo-root ``.env.template`` (``TRAEFIK_DOMAIN`` …).

Kept import-light and Python 3.8-clean: it is imported by the on-NAS CLI (DSM
Python 3.8.12) as well as the 3.12 dashboard container.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import paths, validators

# Substrings that mark a key's *value* as secret (superset of the CLI's historical
# TOKEN/SECRET/PASSWORD masking — adds KEY so e.g. private keys are covered too).
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "KEY")

# Optional components enabled by the presence of a token-style key (non-empty).
_TOKEN_COMPONENTS = {
    "cloudflared": "CLOUDFLARE_TUNNEL_TOKEN",
    "cloudflare_ddns": "CLOUDFLARE_API_TOKEN",
}

# Optional components enabled by a boolean-style key being truthy.
_BOOL_COMPONENTS = {
    "synology_dsm": "SYNOLOGY_DSM_ENABLED",
    "synology_photos": "SYNOLOGY_PHOTOS_ENABLED",
    "synology_drive": "SYNOLOGY_DRIVE_ENABLED",
    "synology_audio": "SYNOLOGY_AUDIO_ENABLED",
    "synology_video": "SYNOLOGY_VIDEO_ENABLED",
}

_TRUTHY = ("true", "1", "yes", "on")

_REDACTED = "****"


def is_secret_key(key: str) -> bool:
    """Return True if a config key's value should be masked."""
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


@dataclass
class RedactedConfig:
    """A safe, structured view of the runtime ``.env``.

    ``values`` has secrets masked when ``read_config(redact=True)`` (the default).
    Empty values are preserved as empty (not masked) so the UI can show that a
    setting exists but is unset.
    """

    values: Dict[str, str]
    enabled_components: Dict[str, bool]
    domain: str
    install_path: Optional[str] = None
    active_version: Optional[str] = None
    env_path: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "install_path": self.install_path,
            "active_version": self.active_version,
            "domain": self.domain,
            "env_path": self.env_path,
            "values": self.values,
            "enabled_components": self.enabled_components,
        }


def read_config(env_path: Optional[Path] = None, redact: bool = True) -> RedactedConfig:
    """Read the runtime ``.env`` into a :class:`RedactedConfig`.

    Args:
        env_path: explicit path to the ``.env`` (defaults to
            ``paths.get_env_path()`` → ``$SYRVIS_HOME/config/.env``).
        redact: mask secret values (default True). Pass False only for internal
            callers that need raw values and never surface them.

    Raises:
        SyrvisHomeError: only when ``env_path`` is None and ``SYRVIS_HOME`` can't
            be resolved. Callers that pass an explicit path never raise here.
    """
    if env_path is None:
        env_path = paths.get_env_path()

    raw = validators.parse_env_file(env_path)

    values: Dict[str, str] = {}
    for key, value in raw.items():
        if redact and value and is_secret_key(key):
            values[key] = _REDACTED
        else:
            values[key] = value

    enabled: Dict[str, bool] = {}
    for name, key in _TOKEN_COMPONENTS.items():
        enabled[name] = bool(raw.get(key, "").strip())
    for name, key in _BOOL_COMPONENTS.items():
        enabled[name] = raw.get(key, "").strip().lower() in _TRUTHY

    # Best-effort install metadata — never let these fail a config read.
    install_path = None
    active_version = None
    try:
        install_path = str(paths.get_syrvis_home())
    except Exception:
        pass
    try:
        active_version = paths.get_active_version()
    except Exception:
        pass

    return RedactedConfig(
        values=values,
        enabled_components=enabled,
        domain=raw.get("DOMAIN", ""),
        install_path=install_path,
        active_version=active_version,
        env_path=str(env_path),
    )
