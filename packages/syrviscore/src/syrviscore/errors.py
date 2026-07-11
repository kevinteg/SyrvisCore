"""
Typed error taxonomy for the SyrvisCore service package.

Every failure mode raises a SyrvisError subclass with a stable machine-readable
``code``. The CLI (and the MCP server / dashboard adapters built on the --json
contract) catch SyrvisError at the boundary and render it for humans or as
JSON; library code never prints or exits.

Mirrors ``syrviscore_manager.errors``. The concrete exception classes live in
the modules that raise them (``docker_manager.DockerError``,
``paths.SyrvisHomeError``, ``stack.StackError``,
``service_schema.ServiceValidationError``, ``privileged_ops.PrivilegedOpsError``)
so existing imports keep working; they all subclass this base.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""


class SyrvisError(Exception):
    """Base class for all service-package errors.

    Attributes:
        code: Stable machine-readable identifier (part of the adapter contract).
        exit_code: Process exit code the CLI boundary uses for this error.
    """

    code = "error"
    exit_code = 1

    def to_dict(self):
        """Machine-readable envelope for the --json / MCP contract."""
        return {"error": str(self), "code": self.code}
