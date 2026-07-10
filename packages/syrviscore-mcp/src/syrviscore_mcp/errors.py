"""
Typed error taxonomy for the SyrvisCore MCP server.

Every failure raises an McpError subclass carrying a stable ``code`` and an
``operator_hint`` — an actionable next step for whoever runs the operator
session. The server surfaces these as tool errors; the model sees the message
and hint, never a raw traceback or an unredacted command line.
"""

from typing import Optional


class McpError(Exception):
    """Base class for all MCP-layer errors."""

    code = "error"

    def __init__(self, message: str, operator_hint: str = "", detail: Optional[str] = None):
        super().__init__(message)
        self.operator_hint = operator_hint
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": str(self)}
        if self.operator_hint:
            d["hint"] = self.operator_hint
        if self.detail:
            d["detail"] = self.detail
        return d


# --- Input / guardrail errors (never reach SSH) ---


class ValidationError(McpError):
    """An argument failed the MCP's input validation (injection guard)."""

    code = "validation"


class SandboxError(McpError):
    """The target service/container is not managed by SyrvisCore."""

    code = "sandbox"


class ConfirmationError(McpError):
    """A destructive tool was called without a valid confirmation token."""

    code = "confirmation_required"


# --- Remote execution errors ---


class ConfigError(McpError):
    """The server config or a remote binary path is wrong."""

    code = "config"


class NetworkError(McpError):
    """SSH could not reach the NAS (host down / timeout)."""

    code = "network"


class AuthError(McpError):
    """SSH authentication failed (key not accepted)."""

    code = "auth"


class HostKeyError(McpError):
    """The NAS host key did not match the pinned known_hosts entry."""

    code = "host_key"


class PrivilegeError(McpError):
    """sudo refused the command (not enumerated, or NOPASSWD misconfigured)."""

    code = "privilege"


class ProtocolError(McpError):
    """A command that should emit JSON did not."""

    code = "protocol"


class CliError(McpError):
    """The remote CLI ran but returned a non-zero exit for an application reason."""

    code = "cli"

    def __init__(self, message: str, returncode: int, operator_hint: str = "", detail=None):
        super().__init__(message, operator_hint=operator_hint, detail=detail)
        self.returncode = returncode

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["returncode"] = self.returncode
        return d
