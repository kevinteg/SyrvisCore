"""
FastMCP server — the thin adapter over tools.py.

Each MCP tool is a near-mechanical projection of a SyrvisCore CLI command. The
server holds a single lazily-built ToolContext (config + SSH runner + token
secret) and forwards to the tested logic in tools.py. Every tool returns a dict;
McpErrors are surfaced as structured error dicts (never a raw traceback).

MCP annotation hints:
- readOnlyHint: the tool does not change NAS state.
- destructiveHint: the tool changes state irreversibly and requires a
  confirmation token (two-call handshake).
- idempotentHint: safe to repeat.
- openWorldHint: reaches beyond the NAS (GitHub / a remote git repo).
"""

from typing import Optional

from fastmcp import FastMCP

from . import tools
from .config import load_config
from .errors import McpError
from .remote import RemoteRunner

mcp = FastMCP("syrviscore")

_ctx: Optional[tools.ToolContext] = None


def get_context() -> tools.ToolContext:
    global _ctx
    if _ctx is None:
        cfg = load_config()
        _ctx = tools.ToolContext(cfg=cfg, runner=RemoteRunner(cfg), secret=cfg.token_secret())
    return _ctx


def _call(fn, **kwargs) -> dict:
    ctx = get_context()
    try:
        return fn(ctx, **kwargs)
    except McpError as e:
        # Record rejected/attacked calls too — validation/sandbox/token failures
        # happen before the remote runner would log anything, and they are the
        # events a defender most wants to see (G16).
        ctx.runner.audit_event(fn.__name__, kwargs, type(e).__name__)
        return e.to_dict()


# ----------------------------- read-only -----------------------------

RO = {"readOnlyHint": True}


@mcp.tool(annotations=RO)
def status() -> dict:
    """Core service status: active version and each core container's state."""
    return _call(tools.status)


@mcp.tool(annotations=RO)
def verify(smoke: bool = False) -> dict:
    """Health + desired-vs-actual drift report (read-only). smoke=fast subset."""
    return _call(tools.verify, smoke=smoke)


@mcp.tool(annotations=RO)
def service_list() -> dict:
    """List the Layer 2 services SyrvisCore manages (name/version/status/url)."""
    return _call(tools.service_list)


@mcp.tool(annotations=RO)
def stack_hostnames() -> dict:
    """Required external DNS/tunnel state: every routed hostname, its exposure,
    and the record to create (LAN A record for 'internal'; Cloudflare Tunnel +
    Access for 'tunnel'). The seam a deployment reconciles against (read-only)."""
    return _call(tools.stack_hostnames)


@mcp.tool(annotations=RO)
def logs(service: Optional[str] = None, tail: int = 100) -> dict:
    """Recent log lines for a core/managed service (bounded; never streaming)."""
    return _call(tools.logs, service=service, tail=tail)


@mcp.tool(annotations=RO)
def versions_list() -> dict:
    """Installed service versions and which is active."""
    return _call(tools.versions_list)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def check_updates() -> dict:
    """Check GitHub for a newer service release (read-only)."""
    return _call(tools.check_updates)


@mcp.tool(annotations=RO)
def info() -> dict:
    """Installation info: manager version, home, active version, setup state."""
    return _call(tools.info)


@mcp.tool(annotations=RO)
def backup_list() -> dict:
    """List available backup archives."""
    return _call(tools.backup_list)


@mcp.tool(annotations=RO)
def cleanup_preview(keep: int = 2) -> dict:
    """Preview which old versions a cleanup would remove (dry-run; read-only)."""
    return _call(tools.cleanup_preview, keep=keep)


# ----------------------- privileged, non-destructive -----------------------


@mcp.tool
def start() -> dict:
    """Start the core services (privileged)."""
    return _call(tools.start)


@mcp.tool
def stop() -> dict:
    """Stop the core services (privileged; non-destructive)."""
    return _call(tools.stop)


@mcp.tool(annotations={"idempotentHint": True})
def restart() -> dict:
    """Restart the core services (privileged)."""
    return _call(tools.restart)


@mcp.tool(annotations={"idempotentHint": True})
def verify_fix(smoke: bool = False) -> dict:
    """Apply sanctioned remediations then re-report health (privileged)."""
    return _call(tools.verify_fix, smoke=smoke)


@mcp.tool(annotations={"idempotentHint": True})
def stack_apply() -> dict:
    """Regenerate docker-compose.yaml from the declared stack (privileged;
    idempotent). Run start/restart afterward to apply the new compose."""
    return _call(tools.stack_apply)


@mcp.tool
def service_start(name: str) -> dict:
    """Start a managed Layer 2 service by name (privileged)."""
    return _call(tools.service_start, name=name)


@mcp.tool
def service_stop(name: str) -> dict:
    """Stop a managed Layer 2 service by name (privileged; non-destructive)."""
    return _call(tools.service_stop, name=name)


@mcp.tool(annotations={"idempotentHint": True})
def service_update(name: str) -> dict:
    """Update a managed Layer 2 service from its git repo (privileged)."""
    return _call(tools.service_update, name=name)


@mcp.tool(annotations={"openWorldHint": True, "destructiveHint": True})
def service_add(git_url: str, confirm: str = "") -> dict:
    """Add a Layer 2 service from a git URL (privileged; clones + RUNS new code).
    Fails closed unless the host is in safety.git_url_allowed_hosts. Two-call:
    first returns a plan+token; re-call with confirm=<token> to proceed."""
    return _call(tools.service_add, git_url=git_url, confirm=confirm)


@mcp.tool(annotations={"openWorldHint": True, "destructiveHint": True})
def service_run(
    name: str,
    image: str,
    subdomain: str = "",
    exposure: str = "internal",
    port: int = 80,
    confirm: str = "",
) -> dict:
    """Run a Layer 2 service from a published image (privileged; pulls + RUNS an
    image). exposure='tunnel' exposes it remotely via Cloudflare. subdomain
    defaults to name. Fails closed unless the registry is in
    safety.image_allowed_registries. Two-call: first returns a plan+token;
    re-call with confirm=<token> to proceed."""
    return _call(
        tools.service_run,
        name=name,
        image=image,
        subdomain=subdomain or None,
        exposure=exposure,
        port=port,
        confirm=confirm,
    )


@mcp.tool(annotations={"openWorldHint": True})
def install(version: Optional[str] = None) -> dict:
    """Install a service version (latest if omitted; additive; privileged).

    Reaches GitHub to download the release, so it carries openWorldHint (matching
    check_updates); additive/non-destructive, so no destructiveHint.
    """
    return _call(tools.install, version=version)


# ----------------------- privileged + destructive -----------------------

DESTRUCTIVE = {"destructiveHint": True}


@mcp.tool(annotations=DESTRUCTIVE)
def activate(version: str, confirm: str = "") -> dict:
    """Switch the active service version. Two-call: first returns a plan+token;
    re-call with confirm=<token> to apply. (privileged, state-changing)."""
    return _call(tools.activate, version=version, confirm=confirm)


@mcp.tool(annotations=DESTRUCTIVE)
def rollback(version: Optional[str] = None, confirm: str = "") -> dict:
    """Full restore to a previous version from backup. Two-call handshake.
    (privileged, destructive)."""
    return _call(tools.rollback, version=version, confirm=confirm)


@mcp.tool(annotations=DESTRUCTIVE)
def uninstall(version: str, confirm: str = "") -> dict:
    """Remove an installed (non-active) version. Two-call handshake.
    (privileged, destructive)."""
    return _call(tools.uninstall, version=version, confirm=confirm)


@mcp.tool(annotations=DESTRUCTIVE)
def cleanup(keep: int = 2, confirm: str = "") -> dict:
    """Remove old versions, keeping the newest N. Two-call handshake.
    (privileged, destructive)."""
    return _call(tools.cleanup, keep=keep, confirm=confirm)


@mcp.tool(annotations=DESTRUCTIVE)
def service_remove(name: str, confirm: str = "") -> dict:
    """Remove a managed Layer 2 service (data preserved; purge is not automatable).
    Two-call handshake. (privileged, destructive)."""
    return _call(tools.service_remove, name=name, confirm=confirm)
