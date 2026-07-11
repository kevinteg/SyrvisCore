"""
Tool logic — the fastmcp-free core.

Each function here is the actual behavior of an MCP tool: validate args, enforce
sandbox membership, run the confirmation handshake for destructive ops, invoke
the remote command, and (for the manager mutators that lack --json) follow up
with a read for ground truth. server.py is a thin FastMCP wrapper that exposes
these with typed signatures and hints; keeping the logic here makes it all
unit-testable without fastmcp or a NAS.
"""

import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from . import sandbox, tokens, validate
from .commands import get_command
from .config import NASConfig
from .errors import McpError
from .remote import RemoteRunner


@dataclass
class ToolContext:
    cfg: NASConfig
    runner: RemoteRunner
    secret: bytes
    used_nonces: set = field(default_factory=set)
    now: Callable[[], float] = time.time
    nonce_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        # Mix a per-process random salt into the configured secret so a server
        # restart changes the effective signing key and voids every outstanding
        # confirmation token (the invariant tokens.py documents). In-memory
        # used_nonces alone can't survive a restart; this closes that replay gap.
        self.secret = hashlib.blake2b(self.secret, salt=os.urandom(16), digest_size=32).digest()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _run(ctx: ToolContext, command_id: str, args: Optional[Dict] = None) -> Dict:
    return ctx.runner.run(get_command(command_id), args or {})


def _with_version_state(ctx: ToolContext, result: Dict) -> Dict:
    """Attach the post-op {versions, active} as ground truth for non-JSON mutators."""
    try:
        after = _run(ctx, "versions_list")
        result["versions"] = after.get("versions")
        result["active"] = after.get("active")
    except McpError:
        pass
    return result


def _with_service_state(ctx: ToolContext, result: Dict) -> Dict:
    try:
        after = _run(ctx, "service_list")
        result["services"] = after.get("services")
    except McpError:
        pass
    return result


def _confirm_or_plan(
    ctx: ToolContext, tool: str, bound_args: Dict, confirm: str, state_parts: list, plan: Dict
):
    """Run the destructive-op confirmation handshake.

    Returns a plan+token dict when confirmation is still needed (no mutation),
    or None once a valid token has been verified (caller proceeds to mutate).
    """
    state = tokens.state_hash(*state_parts)
    if not confirm:
        nonce = secrets.token_hex(8)
        expiry = int(ctx.now()) + ctx.cfg.token_ttl_s
        token = tokens.mint(ctx.secret, tool, bound_args, state, nonce, expiry)
        return {
            "needs_confirmation": True,
            "plan": plan,
            "confirm_token": token,
            "expires_at": expiry,
            "note": "re-call this tool with confirm=<confirm_token> to proceed",
        }
    tokens.verify(
        ctx.secret,
        tool,
        bound_args,
        state,
        confirm,
        ctx.now(),
        ctx.used_nonces,
        lock=ctx.nonce_lock,
    )
    return None


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------


def status(ctx: ToolContext) -> Dict:
    return _run(ctx, "status")


def verify(ctx: ToolContext, smoke: bool = False) -> Dict:
    return _run(ctx, "verify_smoke" if smoke else "verify")


def service_list(ctx: ToolContext) -> Dict:
    return _run(ctx, "service_list")


def stack_hostnames(ctx: ToolContext) -> Dict:
    return _run(ctx, "stack_hostnames")


def logs(ctx: ToolContext, service: Optional[str] = None, tail: int = 100) -> Dict:
    validate.validate_tail(tail)
    if service is not None:
        validate.validate_name(service)
        sandbox.assert_service_managed(ctx.runner, service)
    return _run(ctx, "logs", {"service": service, "tail": tail})


def versions_list(ctx: ToolContext) -> Dict:
    return _run(ctx, "versions_list")


def check_updates(ctx: ToolContext) -> Dict:
    return _run(ctx, "check_updates")


def info(ctx: ToolContext) -> Dict:
    return _run(ctx, "info")


def backup_list(ctx: ToolContext) -> Dict:
    return _run(ctx, "backup_list")


def cleanup_preview(ctx: ToolContext, keep: int = 2) -> Dict:
    validate.validate_keep(keep)
    return _run(ctx, "cleanup_preview", {"keep": keep})


# --------------------------------------------------------------------------
# privileged, non-destructive tools
# --------------------------------------------------------------------------


def start(ctx: ToolContext) -> Dict:
    return _run(ctx, "start")


def stop(ctx: ToolContext) -> Dict:
    return _run(ctx, "stop")


def restart(ctx: ToolContext) -> Dict:
    return _run(ctx, "restart")


def verify_fix(ctx: ToolContext, smoke: bool = False) -> Dict:
    return _run(ctx, "verify_fix_smoke" if smoke else "verify_fix")


def service_start(ctx: ToolContext, name: str) -> Dict:
    validate.validate_name(name)
    sandbox.assert_service_managed(ctx.runner, name)
    return _with_service_state(ctx, _run(ctx, "service_start", {"name": name}))


def service_stop(ctx: ToolContext, name: str) -> Dict:
    validate.validate_name(name)
    sandbox.assert_service_managed(ctx.runner, name)
    return _with_service_state(ctx, _run(ctx, "service_stop", {"name": name}))


def service_update(ctx: ToolContext, name: str) -> Dict:
    validate.validate_name(name)
    sandbox.assert_service_managed(ctx.runner, name)
    return _with_service_state(ctx, _run(ctx, "service_update", {"name": name}))


def service_add(ctx: ToolContext, git_url: str, confirm: str = "") -> Dict:
    # service_add is the one tool that clones and runs NEW code on the NAS, so
    # it fails closed on the host allowlist AND requires a confirmation token.
    validate.validate_git_url(git_url, ctx.cfg.git_url_allowed_hosts)
    current = service_list(ctx)
    plan = {"action": "service_add", "git_url": git_url, "existing": current.get("services")}
    pending = _confirm_or_plan(ctx, "service_add", {"git_url": git_url}, confirm, [git_url], plan)
    if pending:
        return pending
    return _with_service_state(ctx, _run(ctx, "service_add", {"git_url": git_url}))


def service_run(
    ctx: ToolContext,
    name: str,
    image: str,
    subdomain: Optional[str] = None,
    exposure: str = "internal",
    port: int = 80,
    confirm: str = "",
) -> Dict:
    # service_run pulls and RUNS a container image (and may expose it to the
    # internet), so it fails closed on the image registry allowlist AND requires
    # a confirmation token — the same posture as service_add.
    validate.validate_name(name)
    validate.validate_image(image, ctx.cfg.image_allowed_registries)
    sub = validate.validate_subdomain(subdomain) if subdomain else validate.validate_subdomain(name)
    exp = validate.validate_exposure(exposure)
    validate.validate_port(port)
    args = {"name": name, "image": image, "subdomain": sub, "exposure": exp, "port": port}

    current = service_list(ctx)
    plan = {"action": "service_run", **args, "existing": current.get("services")}
    pending = _confirm_or_plan(
        ctx, "service_run", args, confirm, [image, name, sub, exp, port], plan
    )
    if pending:
        return pending
    return _with_service_state(ctx, _run(ctx, "service_run", args))


def install(ctx: ToolContext, version: Optional[str] = None) -> Dict:
    if version is not None:
        validate.validate_version(version)
    return _with_version_state(ctx, _run(ctx, "install", {"version": version}))


# --------------------------------------------------------------------------
# privileged + destructive tools (confirmation token required)
# --------------------------------------------------------------------------


def activate(ctx: ToolContext, version: str, confirm: str = "") -> Dict:
    version = validate.validate_version(version)
    current = versions_list(ctx)
    plan = {"action": "activate", "version": version, "current_active": current.get("active")}
    pending = _confirm_or_plan(ctx, "activate", {"version": version}, confirm, [current], plan)
    if pending:
        return pending
    return _with_version_state(ctx, _run(ctx, "activate", {"version": version}))


def rollback(ctx: ToolContext, version: Optional[str] = None, confirm: str = "") -> Dict:
    if version is not None:
        version = validate.validate_version(version)
    current = versions_list(ctx)
    backups = backup_list(ctx)
    plan = {"action": "rollback", "version": version, "current_active": current.get("active")}
    pending = _confirm_or_plan(
        ctx, "rollback", {"version": version}, confirm, [current, backups], plan
    )
    if pending:
        return pending
    return _with_version_state(ctx, _run(ctx, "rollback", {"version": version}))


def uninstall(ctx: ToolContext, version: str, confirm: str = "") -> Dict:
    version = validate.validate_version(version)
    current = versions_list(ctx)
    plan = {"action": "uninstall", "version": version, "installed": current.get("versions")}
    pending = _confirm_or_plan(ctx, "uninstall", {"version": version}, confirm, [current], plan)
    if pending:
        return pending
    return _with_version_state(ctx, _run(ctx, "uninstall", {"version": version}))


def cleanup(ctx: ToolContext, keep: int = 2, confirm: str = "") -> Dict:
    validate.validate_keep(keep)
    preview = cleanup_preview(ctx, keep)
    plan = {"action": "cleanup", "keep": keep, "would_remove": preview.get("detail")}
    pending = _confirm_or_plan(ctx, "cleanup", {"keep": keep}, confirm, [preview], plan)
    if pending:
        return pending
    return _with_version_state(ctx, _run(ctx, "cleanup", {"keep": keep}))


def service_remove(ctx: ToolContext, name: str, confirm: str = "") -> Dict:
    validate.validate_name(name)
    sandbox.assert_service_managed(ctx.runner, name)
    current = service_list(ctx)
    entry = next((s for s in current.get("services", []) if s.get("name") == name), None)
    plan = {"action": "service_remove", "name": name, "current": entry, "purge": False}
    pending = _confirm_or_plan(ctx, "service_remove", {"name": name}, confirm, [entry], plan)
    if pending:
        return pending
    return _with_service_state(ctx, _run(ctx, "service_remove", {"name": name}))
