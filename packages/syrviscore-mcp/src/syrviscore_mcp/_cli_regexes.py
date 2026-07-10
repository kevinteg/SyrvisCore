"""
Copies of the SyrvisCore CLI validation regexes.

The MCP server must NOT import the syrviscore/syrviscore-manager packages (they
target Python 3.8 / the NAS and pull in docker, etc.). Instead these patterns are
copied verbatim and pinned identical to the source by tests/test_drift.py (G17) —
so an MCP arg that the CLI would reject cannot slip through validated by a stale
copy, and a change to the CLI's rules fails the drift test until this file is
updated to match.

Sources (kept in sync):
- VERSION_RE:    packages/syrviscore-manager/src/syrviscore_manager/paths.py
- NAME_RE:       packages/syrviscore/src/syrviscore/service_schema.py
- RESERVED_NAMES: packages/syrviscore/src/syrviscore/service_schema.py
"""

import re

# paths.VERSION_RE — validate_version also strips one optional leading 'v'.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# service_schema.NAME_RE — a service/container/network identifier.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# service_schema.RESERVED_NAMES — core-stack names a Layer 2 service may not use.
RESERVED_NAMES = frozenset({"traefik", "portainer", "cloudflared", "proxy", "syrvis-macvlan"})


def validate_version_str(version: str) -> str:
    """Mirror of paths.validate_version: accept a single leading 'v', require N.N.N."""
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a non-empty string")
    v = version[1:] if version.startswith("v") else version
    if not VERSION_RE.match(v):
        raise ValueError(f"invalid version {version!r}: expected MAJOR.MINOR.PATCH")
    return v
