"""Back-compat re-exports: the command registry lives in the platform now.

The single source of truth for every remote command a seam client may run is
``syrviscore.seam.registry`` (the enumerated sudoers boundary, the shim
allowlist, and the runtime argv are all derived from it — G18). The MCP is a
generated consumer of that registry; this module keeps the historical
``syrviscore_mcp.commands`` import surface working.
"""

from syrviscore.seam.registry import (  # noqa: F401
    COMMANDS,
    COMMANDS_BY_ID,
    DESTRUCTIVE_IDS,
    KIND_BOOLEAN,
    KIND_EXPOSURE,
    KIND_GIT_URL,
    KIND_IMAGE,
    KIND_KEEP,
    KIND_NAME,
    KIND_PORT,
    KIND_PRUNE_POLICY,
    KIND_SHED_REASON,
    KIND_SUBDOMAIN,
    KIND_TAIL,
    KIND_TIMESTAMP,
    KIND_VERSION,
    Command,
    FlagValue,
    Slot,
    get_command,
)
