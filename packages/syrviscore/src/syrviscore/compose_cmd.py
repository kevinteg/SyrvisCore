"""Resolve the docker compose command this host actually provides.

Docker Compose ships two ways: the v2 plugin (``docker compose``) and the v1
standalone (``docker-compose``). Synology DSM has varied — older Container
Manager only has the v1 standalone, newer has the v2 plugin. Rather than hardcode
one (which broke Layer 2 on a v1-only NAS while the core stack used v1 and worked),
probe once for whichever is present and reuse it everywhere.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import subprocess
from typing import List, Optional

# Preference order: the v2 plugin first (the modern default), then v1 standalone.
_CANDIDATES = (["docker", "compose"], ["docker-compose"])

_cached: Optional[List[str]] = None


def resolve_compose_cmd() -> List[str]:
    """Return the base argv for the available compose command (cached).

    Probes ``<candidate> version``; the first that exits 0 wins. If neither
    resolves (no docker at all), returns the v2 form so the downstream call fails
    with a clear docker error rather than a confusing one here.
    """
    global _cached
    if _cached is not None:
        return list(_cached)
    for candidate in _CANDIDATES:
        try:
            result = subprocess.run(
                candidate + ["version"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            _cached = candidate
            return list(_cached)
    _cached = list(_CANDIDATES[0])
    return list(_cached)


def reset_cache() -> None:
    """Clear the memoized resolution (tests)."""
    global _cached
    _cached = None
