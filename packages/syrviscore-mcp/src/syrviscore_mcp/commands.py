"""
The single source of truth for every remote command the MCP may run (G18).

Both the runtime (remote.py builds an ssh argv from a Command) and the deploy
generator (deploy/gen.py renders the sudoers file and the forced-command shim)
consume this same registry, so the enumerated sudoers boundary, the shim
allowlist, and the actual argv can never drift apart. A drift test asserts the
committed sudoers/shim match what gen.py produces from this registry.

Command construction rules (mirroring the real CLI, verified against the code):
- `syrvis` commands run via the WRAPPER (it exports SYRVIS_HOME) — no --path.
- `syrvisctl` commands run via the venv binary. Only `install` accepts --path;
  the others resolve SYRVIS_HOME via the single-install volume scan (works
  under sudo in production). So we pass --path ONLY where the CLI supports it.
- A user-supplied positional value is always placed after a literal `--`
  separator, after all server-controlled flags/options (so it can never be
  parsed as a flag). Flag-VALUE slots (e.g. `--keep N`) are server-gated ints.
- Optional positionals expand to TWO accepted forms (present / absent) in the
  sudoers + shim allowlist.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# Slot kinds — how the user value for a slot is validated (see validate.py).
KIND_VERSION = "version"
KIND_NAME = "name"
KIND_GIT_URL = "git_url"
KIND_KEEP = "keep"
KIND_TAIL = "tail"
KIND_IMAGE = "image"
KIND_SUBDOMAIN = "subdomain"
KIND_EXPOSURE = "exposure"
KIND_PORT = "port"


@dataclass(frozen=True)
class Slot:
    name: str
    kind: str
    optional: bool = False


@dataclass(frozen=True)
class Command:
    id: str
    cli: str  # "syrvis" | "syrvisctl"
    subcommand: List[str]  # literal tokens after the binary, e.g. ["service", "stop"]
    sudo: bool = False
    read_only: bool = False
    destructive: bool = False
    expect_json: bool = True
    flags: List[object] = field(default_factory=list)  # literals + FlagValue Slots (e.g. --keep N)
    install_path: bool = False  # append --path <home> (only 'install' supports it)
    positional: Optional[Slot] = None  # user value placed after '--'
    timeout_s: int = 120


# FlagValue: a Slot that supplies the value of a preceding literal flag
# (e.g. --keep {keep}). Distinct from `positional` which goes after '--'.
@dataclass(frozen=True)
class FlagValue:
    slot: Slot


COMMANDS: List[Command] = [
    # ---- read-only (no sudo, no token) ----
    Command("status", "syrvis", ["status"], read_only=True, flags=["--json"]),
    Command("verify", "syrvis", ["verify"], read_only=True, flags=["--json"]),
    Command("verify_smoke", "syrvis", ["verify"], read_only=True, flags=["--smoke", "--json"]),
    Command("service_list", "syrvis", ["service", "list"], read_only=True, flags=["--json"]),
    Command(
        "logs",
        "syrvis",
        ["logs"],
        read_only=True,
        expect_json=False,
        flags=["-n", FlagValue(Slot("tail", KIND_TAIL))],
        positional=Slot("service", KIND_NAME, optional=True),
    ),
    Command("stack_hostnames", "syrvis", ["stack", "hostnames"], read_only=True, flags=["--json"]),
    Command("versions_list", "syrvisctl", ["list"], read_only=True, flags=["--json"]),
    Command("check_updates", "syrvisctl", ["check"], read_only=True, flags=["--json"]),
    Command("info", "syrvisctl", ["info"], read_only=True, flags=["--json"]),
    Command("backup_list", "syrvisctl", ["backup", "list"], read_only=True, flags=["--json"]),
    Command(
        "cleanup_preview",
        "syrvisctl",
        ["cleanup"],
        read_only=True,
        expect_json=False,
        flags=["--keep", FlagValue(Slot("keep", KIND_KEEP)), "--dry-run"],
    ),
    # ---- privileged, non-destructive (sudo, no token) ----
    Command("start", "syrvis", ["start"], sudo=True, expect_json=False),
    Command("stop", "syrvis", ["stop"], sudo=True, expect_json=False),
    Command("restart", "syrvis", ["restart"], sudo=True, expect_json=False),
    Command("stack_apply", "syrvis", ["stack", "apply"], sudo=True, expect_json=False),
    Command("verify_fix", "syrvis", ["verify"], sudo=True, flags=["--fix", "--json"]),
    Command(
        "verify_fix_smoke", "syrvis", ["verify"], sudo=True, flags=["--smoke", "--fix", "--json"]
    ),
    Command(
        "service_start",
        "syrvis",
        ["service", "start"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_stop",
        "syrvis",
        ["service", "stop"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_update",
        "syrvis",
        ["service", "update"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_add",
        "syrvis",
        ["service", "add"],
        sudo=True,
        expect_json=False,
        positional=Slot("git_url", KIND_GIT_URL),
        timeout_s=600,
    ),
    Command(
        "service_run",
        "syrvis",
        ["service", "run"],
        sudo=True,
        expect_json=False,
        # Fixed flag order mirrors remote.build_remote_tokens so the shim matches
        # the real argv exactly. name is the trailing positional (after '--').
        flags=[
            "--image",
            FlagValue(Slot("image", KIND_IMAGE)),
            "--subdomain",
            FlagValue(Slot("subdomain", KIND_SUBDOMAIN)),
            "--exposure",
            FlagValue(Slot("exposure", KIND_EXPOSURE)),
            "--port",
            FlagValue(Slot("port", KIND_PORT)),
        ],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    Command(
        "install",
        "syrvisctl",
        ["install"],
        sudo=True,
        expect_json=False,
        flags=["-y"],
        install_path=True,
        positional=Slot("version", KIND_VERSION, optional=True),
        timeout_s=600,
    ),
    # ---- privileged + destructive (sudo, confirmation token) ----
    Command(
        "activate",
        "syrvisctl",
        ["activate"],
        sudo=True,
        destructive=True,
        expect_json=False,
        positional=Slot("version", KIND_VERSION),
    ),
    Command(
        "rollback",
        "syrvisctl",
        ["rollback"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("version", KIND_VERSION, optional=True),
    ),
    Command(
        "uninstall",
        "syrvisctl",
        ["uninstall"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("version", KIND_VERSION),
    ),
    Command(
        "cleanup",
        "syrvisctl",
        ["cleanup"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["--keep", FlagValue(Slot("keep", KIND_KEEP)), "-y"],
    ),
    Command(
        "service_remove",
        "syrvis",
        ["service", "remove"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("name", KIND_NAME),
    ),
]

COMMANDS_BY_ID = {c.id: c for c in COMMANDS}

DESTRUCTIVE_IDS = frozenset(c.id for c in COMMANDS if c.destructive)


def get_command(cmd_id: str) -> Command:
    if cmd_id not in COMMANDS_BY_ID:
        raise KeyError(f"unknown command id: {cmd_id}")
    return COMMANDS_BY_ID[cmd_id]
