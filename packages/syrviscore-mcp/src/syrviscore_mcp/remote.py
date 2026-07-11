"""
Injection-safe remote execution over SSH.

Core rule (G1): no shell string is ever *built by us*. We construct an argv list
of already-validated tokens, shlex.quote each one, and hand the result to ssh as
a single remote command. Every user value has passed validate.py first, and the
NAS-side forced-command shim independently re-validates $SSH_ORIGINAL_COMMAND.

This module also classifies the remote result into the typed error taxonomy and
writes an audit line per call (G16). The subprocess callable is injectable so the
whole thing is unit-testable without a NAS.
"""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import validate
from .commands import Command, FlagValue, Slot
from .config import NASConfig
from .errors import (
    AuthError,
    CliError,
    ConfigError,
    HostKeyError,
    NetworkError,
    PrivilegeError,
    ProtocolError,
    ValidationError,
)

_KIND_VALIDATORS = {
    "version": lambda v, cfg: validate.validate_version(v),
    "name": lambda v, cfg: validate.validate_name(v),
    "git_url": lambda v, cfg: validate.validate_git_url(v, cfg.git_url_allowed_hosts),
    "keep": lambda v, cfg: str(validate.validate_keep(v)),
    "tail": lambda v, cfg: str(validate.validate_tail(v)),
    "image": lambda v, cfg: validate.validate_image(v, cfg.image_allowed_registries),
    "subdomain": lambda v, cfg: validate.validate_subdomain(v),
    "exposure": lambda v, cfg: validate.validate_exposure(v),
    "port": lambda v, cfg: str(validate.validate_port(v)),
}


def resolve_slot(cfg: NASConfig, slot: Slot, args: Dict) -> Optional[str]:
    """Validate and stringify a user-supplied slot value (None if optional+absent)."""
    value = args.get(slot.name)
    if value is None:
        if slot.optional:
            return None
        raise ValidationError(f"missing required argument: {slot.name}")
    validator = _KIND_VALIDATORS[slot.kind]
    return validator(value, cfg)


def build_remote_tokens(cfg: NASConfig, command: Command, args: Dict) -> List[str]:
    """Build the ordered list of remote-command tokens for a Command (G7/G8).

    User positionals are validated and placed after a literal ``--``; flag values
    are validated ints; fixed flags are literals. Nothing user-derived becomes a
    flag or a shell metacharacter.
    """
    binary = cfg.syrvis_wrapper if command.cli == "syrvis" else cfg.syrvisctl_path

    tokens: List[str] = []
    if command.sudo and cfg.use_sudo:
        tokens += [cfg.sudo_binary, "-n"]
    tokens.append(binary)
    tokens += list(command.subcommand)

    for flag in command.flags:
        if isinstance(flag, str):
            tokens.append(flag)
        elif isinstance(flag, FlagValue):
            tokens.append(resolve_slot(cfg, flag.slot, args))  # required int -> str
        else:  # pragma: no cover - registry integrity
            raise ConfigError(f"bad flag spec: {flag!r}")

    if command.install_path:
        tokens += ["--path", cfg.syrvis_home]

    if command.positional is not None:
        value = resolve_slot(cfg, command.positional, args)
        if value is not None:
            # '--' terminates option parsing so the value can never be a flag.
            tokens += ["--", value]

    # Every user value was metachar-checked by its kind validator (resolve_slot);
    # the remaining tokens are our own trusted literals (binary path, flags, --).
    return tokens


def base_ssh(cfg: NASConfig) -> List[str]:
    return [
        "ssh",
        "-F",
        str(cfg.ssh_config_file),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={cfg.control_path}",
        "-o",
        "ControlPersist=60",
        cfg.ssh_target,
    ]


def build_ssh_argv(cfg: NASConfig, remote_tokens: List[str]) -> List[str]:
    """The full local ssh argv: BASE_SSH + one quoted remote-command string."""
    remote_str = " ".join(shlex.quote(t) for t in remote_tokens)
    return base_ssh(cfg) + [remote_str]


@dataclass
class RunResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str


def classify(result: RunResult, expect_json: bool) -> Dict:
    """Map a remote result to structured output or a typed error."""
    rc, out, err = result.returncode, result.stdout or "", result.stderr or ""
    low = err.lower()

    if rc == 255:
        if any(s in low for s in ("host key", "known_hosts", "remote host identification")):
            raise HostKeyError(
                "NAS host key did not match the pinned entry",
                operator_hint="if the NAS was rebuilt, update known_hosts deliberately",
            )
        if any(s in low for s in ("permission denied", "publickey", "authentication")):
            raise AuthError(
                "SSH authentication failed",
                operator_hint="check the operator key is installed in authorized_keys",
            )
        raise NetworkError(
            "could not reach the NAS over SSH",
            operator_hint="check the NAS is up and reachable",
            detail=err.strip()[:300],
        )

    if rc == 127:
        raise ConfigError(
            "remote binary not found",
            operator_hint="check layout.syrvis_wrapper / layout.syrvisctl_path in config",
            detail=err.strip()[:300],
        )

    if any(s in low for s in ("password is required", "terminal is required", "askpass")):
        raise PrivilegeError(
            "sudo requires a password (NOPASSWD misconfigured)",
            operator_hint="install deploy/sudoers.d/syrviscore-mcp on the NAS",
        )
    if "not allowed" in low or ("sudo" in low and "sudoers" in low):
        raise PrivilegeError(
            "command not permitted by the sudoers policy",
            operator_hint="this operation is intentionally outside the MCP boundary",
        )

    if expect_json:
        try:
            # verify emits valid JSON even at rc==1 (unhealthy) — honor the dict.
            return json.loads(out)
        except (json.JSONDecodeError, ValueError):
            if rc == 0:
                raise ProtocolError(
                    "expected JSON output but got none",
                    detail=out.strip()[:500] or err.strip()[:500],
                )
            raise CliError((err or out).strip()[:500] or "command failed", rc)

    if rc != 0:
        raise CliError((err or out).strip()[:500] or "command failed", rc)
    return {"ok": True, "detail": out.strip()[-2000:]}


class RemoteRunner:
    """Runs Commands on the NAS over SSH with a lazy, cached health probe."""

    def __init__(
        self,
        cfg: NASConfig,
        subprocess_run: Callable = subprocess.run,
        audit_path: Optional[Path] = None,
    ):
        self.cfg = cfg
        self._run = subprocess_run
        self._health_ok: Optional[bool] = None
        self._health_at = 0.0
        self._audit_path = audit_path or Path(
            os.path.expanduser("~/.config/syrviscore-mcp/audit.jsonl")
        )
        # A ControlMaster socket is 60s of authenticated NAS access; keep its
        # directory owner-only so a co-resident local process can't reuse it.
        try:
            cm_dir = Path(cfg.control_path).expanduser().parent
            cm_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(cm_dir, 0o700)
        except OSError:
            pass

    def _exec(self, argv: List[str], timeout: int) -> RunResult:
        proc = self._run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
        return RunResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=getattr(proc, "stdout", "") or "",
            stderr=getattr(proc, "stderr", "") or "",
        )

    def _write_audit(self, entry: dict) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            # The audit log records every remote argv (git URLs, image refs) and
            # rejected/attacked calls — a security record. Keep the dir and file
            # owner-only, matching the ControlMaster hardening above. Enforce the
            # file mode at creation via os.open (chmod-after-create leaves a
            # umask window where the file is briefly world-readable).
            try:
                os.chmod(self._audit_path.parent, 0o700)
            except OSError:
                pass
            fd = os.open(str(self._audit_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # auditing must never block an operation

    def _audit(self, command: Command, remote_tokens: List[str], rc: Optional[int], outcome: str):
        self._write_audit(
            {
                "command": command.id,
                "sudo": command.sudo,
                "remote": remote_tokens,
                "rc": rc,
                "outcome": outcome,
            }
        )

    def audit_event(self, tool: str, args: dict, outcome: str) -> None:
        """Record a call rejected before it reached the NAS (validation/sandbox/token)."""
        self._write_audit(
            {"command": tool, "args": args, "rc": None, "outcome": outcome, "rejected": True}
        )

    def run(self, command: Command, args: Optional[Dict] = None) -> Dict:
        args = args or {}
        remote_tokens = build_remote_tokens(self.cfg, command, args)
        ssh_argv = build_ssh_argv(self.cfg, remote_tokens)
        try:
            result = self._exec(ssh_argv, command.timeout_s)
        except subprocess.TimeoutExpired:
            self._audit(command, remote_tokens, None, "timeout")
            # A non-read-only op that timed out may have partially applied.
            hint = (
                "run 'verify' or 'status' before retrying — the operation state is unknown"
                if not command.read_only
                else "the NAS did not respond in time"
            )
            raise NetworkError(
                f"remote command timed out after {command.timeout_s}s",
                operator_hint=hint,
                detail=("indeterminate" if not command.read_only else None),
            )
        try:
            out = classify(result, command.expect_json)
            self._audit(command, remote_tokens, result.returncode, "ok")
            return out
        except Exception as e:
            self._audit(command, remote_tokens, result.returncode, type(e).__name__)
            raise
