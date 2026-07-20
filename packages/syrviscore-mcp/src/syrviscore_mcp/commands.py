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
KIND_PRUNE_POLICY = "prune_policy"
KIND_BOOLEAN = "boolean"


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
    # schedule list parses the managed crontab block + jobs.d — read-only. It runs
    # under sudo so the 0600-ish jobs.d declarations are readable over the seam,
    # but the CLI itself performs no privileged action (like reconcile_plan).
    Command(
        "schedule_list",
        "syrvis",
        ["schedule", "list"],
        sudo=True,
        read_only=True,
        flags=["--json"],
    ),
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
    # reconcile --dry-run is READ-ONLY by construction (side-effect-free plan);
    # it runs under sudo only so the 0600 services.d declaration files are
    # readable over the seam — the CLI itself skips privilege elevation here.
    Command(
        "reconcile_plan",
        "syrvis",
        ["reconcile"],
        sudo=True,
        read_only=True,
        flags=["--dry-run", "--json"],
    ),
    # WITHOUT --prune, reconcile never removes anything (non-destructive, like
    # verify_fix): it converges to config/services.d declarations only.
    Command("reconcile", "syrvis", ["reconcile"], sudo=True, flags=["--json", "-y"], timeout_s=600),
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
    # service declare authors a services.d declaration through the schema trust
    # boundary and applies NOTHING (reconcile applies later) — non-destructive.
    # Fixed flag order mirrors remote.build_remote_tokens so the shim matches
    # the real argv exactly. name is the trailing positional (after '--').
    Command(
        "service_declare",
        "syrvis",
        ["service", "declare"],
        sudo=True,
        flags=[
            "--image",
            FlagValue(Slot("image", KIND_IMAGE)),
            "--subdomain",
            FlagValue(Slot("subdomain", KIND_SUBDOMAIN)),
            "--exposure",
            FlagValue(Slot("exposure", KIND_EXPOSURE)),
            "--port",
            FlagValue(Slot("port", KIND_PORT)),
            "--enabled",
            FlagValue(Slot("enabled", KIND_BOOLEAN)),
            "--critical",
            FlagValue(Slot("critical", KIND_BOOLEAN)),
            "--json",
        ],
        positional=Slot("name", KIND_NAME),
    ),
    # service adopt generates a declaration from an existing install; the
    # install itself is not touched — non-destructive.
    Command(
        "service_adopt",
        "syrvis",
        ["service", "adopt"],
        sudo=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME),
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
    # reconcile --prune additionally acts on installed-but-undeclared services;
    # 'remove'/'purge' are DESTRUCTIVE, so the whole command takes the token.
    Command(
        "reconcile_prune",
        "syrvis",
        ["reconcile"],
        sudo=True,
        destructive=True,
        flags=["--json", "-y", "--prune", FlagValue(Slot("prune", KIND_PRUNE_POLICY))],
        timeout_s=600,
    ),
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
    # schedule apply reconciles config/jobs.d -> jobs/ scripts + the managed
    # /etc/crontab block. It mutates root cron, so it is DESTRUCTIVE (two-call HMAC
    # handshake, like reconcile_prune). It takes NO cron argv: the schedule lives
    # only in jobs.d (its '*'/',' would fail the shim char-allowlist), and the
    # command is derived as jobs/<name> — the operator never supplies either.
    Command(
        "schedule_apply",
        "syrvis",
        ["schedule", "apply"],
        sudo=True,
        destructive=True,
        flags=["--json"],
        timeout_s=600,
    ),
    # schedule sync clones the ONE root-configured source (config/jobs.source),
    # installs its jobs.d declarations + materializes root-owned jobs/<name>
    # scripts, then reconciles the managed /etc/crontab block. It fetches + runs
    # a root-vetted repo and mutates root cron, so it is DESTRUCTIVE (two-call HMAC
    # handshake, like schedule_apply). It takes NO argv: the source is root-owned
    # (the operator cannot pass or influence it), the cron spec lives only in the
    # YAML, and the command is derived as jobs/<name> — nothing operator-supplied.
    Command(
        "schedule_sync",
        "syrvis",
        ["schedule", "sync"],
        sudo=True,
        destructive=True,
        flags=["--json"],
        timeout_s=600,
    ),
    # secret set writes a Layer 2 service's env_file secret atomically as root:root
    # 0600. The secret arrives on stdin ONLY — it is never a CLI argument, never
    # logged, never a token. destructive=False (idempotent per-service overwrite,
    # analogous to service_declare). expect_json=False (plain "wrote <path>" line).
    # No --json flag: apply-immich-secrets only needs the exit code.
    Command(
        "secret_set",
        "syrvis",
        ["secret", "set"],
        sudo=True,
        destructive=False,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    # config set writes a scheduled job's config/<name>.conf atomically as
    # root:root 0600 — the jobs analog of secret_set (which writes a service's
    # env_file). The conf body arrives on stdin ONLY: never a CLI argument, never
    # logged, never a token. destructive=False (idempotent per-job overwrite).
    # expect_json=False (plain "wrote <path>" line). No --json flag: the caller
    # only needs the exit code. The name is gated to a DECLARED job in
    # config/jobs.d/ by the CLI impl (write_config), just as secret_set gates on
    # services.d — so the operator can render a VETTED job's conf but not create
    # confs for arbitrary names.
    Command(
        "config_set",
        "syrvis",
        ["config", "set"],
        sudo=True,
        destructive=False,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
]

COMMANDS_BY_ID = {c.id: c for c in COMMANDS}

DESTRUCTIVE_IDS = frozenset(c.id for c in COMMANDS if c.destructive)


def get_command(cmd_id: str) -> Command:
    if cmd_id not in COMMANDS_BY_ID:
        raise KeyError(f"unknown command id: {cmd_id}")
    return COMMANDS_BY_ID[cmd_id]
