"""Thin helpers over ``syrviscore.config_reader`` for the probe layer.

Centralizes "is this optional component configured?" and raw-value access so no
probe re-implements env parsing. Everything degrades to safe defaults if the
SyrvisCore home can't be resolved (e.g. in a bare test environment).
"""

from typing import Optional


def component_enabled(name: str) -> bool:
    try:
        from syrviscore.config_reader import read_config

        return bool(read_config().enabled_components.get(name, False))
    except Exception:  # noqa: BLE001
        return False


def raw_value(key: str) -> Optional[str]:
    """Un-redacted config value (for authenticated API calls like DDNS). Never logged."""
    try:
        from syrviscore.config_reader import read_config

        value = read_config(redact=False).values.get(key)
        return value or None
    except Exception:  # noqa: BLE001
        return None
