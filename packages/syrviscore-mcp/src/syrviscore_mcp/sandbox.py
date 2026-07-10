"""
Sandbox membership checks (G9/G10).

Before any tool that targets a named Layer 2 service by name (start/stop/update/
remove, and logs of a specific service), we confirm the target is actually a
service SyrvisCore manages — by consulting the read-only ``service list`` (no
sudo, no mutation). This stops the MCP from being used to poke at arbitrary
containers, and refuses core-stack names outright.
"""

from ._cli_regexes import RESERVED_NAMES
from .commands import get_command
from .errors import SandboxError


def managed_service_names(runner) -> set:
    """The set of Layer 2 service names SyrvisCore currently manages."""
    result = runner.run(get_command("service_list"))
    services = result.get("services", []) if isinstance(result, dict) else []
    return {s.get("name") for s in services if isinstance(s, dict) and s.get("name")}


def assert_service_managed(runner, name: str) -> None:
    """Refuse the operation unless ``name`` is a managed Layer 2 service.

    Raises:
        SandboxError: if the name is a reserved core name or is not in the
            managed service inventory.
    """
    if name in RESERVED_NAMES:
        raise SandboxError(
            f"{name!r} is a SyrvisCore core service, not a manageable Layer 2 service",
            operator_hint="core services are controlled with start/stop/restart, not service_*",
        )
    names = managed_service_names(runner)
    if name not in names:
        raise SandboxError(
            f"service {name!r} is not managed by SyrvisCore",
            operator_hint="call service_list to see the services the MCP may act on",
        )
