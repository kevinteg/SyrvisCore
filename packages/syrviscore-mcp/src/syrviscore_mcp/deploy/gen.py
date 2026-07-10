#!/usr/bin/env python3
"""
Generate the sudoers policy and the forced-command shim from the single command
registry (commands.py). This is the G18 guarantee: the enumerated sudoers
boundary and the shim allowlist are derived from the same source the runtime
uses to build argv, so they cannot drift. A drift test asserts the committed
artifacts equal this generator's output.

Usage:
    python -m syrviscore_mcp.deploy.gen sudoers   > sudoers.d/syrviscore-mcp
    python -m syrviscore_mcp.deploy.gen shim      > ssh/syrvis-mcp-shim
    python -m syrviscore_mcp.deploy.gen check <dir>   # verify committed == generated
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running both as a module and as a script.
try:
    from syrviscore_mcp.commands import COMMANDS, Command, FlagValue
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from syrviscore_mcp.commands import COMMANDS, Command, FlagValue


@dataclass(frozen=True)
class DeployConfig:
    """The deployment-specific paths baked into the sudoers policy and the shim.

    sudo matches commands by their ABSOLUTE PATH, so these must be the real
    paths on YOUR NAS. They default to a /volume1 install. If the SPK installed
    SyrvisCore on a different volume (e.g. /volume4/syrviscore — check with
    `syrvisctl info`), generate with `--home /volume4/syrviscore` so the policy
    matches the real command paths; otherwise sudo denies every command.
    """

    operator: str = "syrvis-operator"
    syrvis_home: str = "/volume1/syrviscore"
    syrvisctl_path: str = "/var/packages/syrviscore/target/venv/bin/syrvisctl"
    shim_path: str = "/usr/local/bin/syrvis-mcp-shim"

    @property
    def syrvis_wrapper(self) -> str:
        return f"{self.syrvis_home}/bin/syrvis"


DEFAULT = DeployConfig()


def _binary(cmd: Command, cfg: DeployConfig) -> str:
    return cfg.syrvis_wrapper if cmd.cli == "syrvis" else cfg.syrvisctl_path


def command_patterns(cmd: Command, cfg: DeployConfig) -> list:
    """All accepted argv shapes for a command (list of token-lists, no sudo).

    Optional positionals expand to two forms (present with '-- *', and absent).
    """
    fixed = [_binary(cmd, cfg)] + list(cmd.subcommand)
    for flag in cmd.flags:
        fixed.append("*" if isinstance(flag, FlagValue) else flag)
    if cmd.install_path:
        fixed += ["--path", cfg.syrvis_home]

    if cmd.positional is None:
        return [fixed]
    with_pos = fixed + ["--", "*"]
    if cmd.positional.optional:
        return [with_pos, list(fixed)]
    return [with_pos]


def render_sudoers(cfg: DeployConfig = DEFAULT) -> str:
    lines = [
        "# /etc/sudoers.d/syrviscore-mcp  (install 0440 root:root; validate: visudo -cf)",
        "# GENERATED from syrviscore_mcp/commands.py by deploy/gen.py — do not hand-edit.",
        "# Enumerated NOPASSWD boundary for the MCP operator. Only the privileged",
        "# SyrvisCore subcommands below are permitted; everything else needs a human.",
        "",
        f'Defaults:{cfg.operator}  !requiretty, env_reset, secure_path="/usr/bin:/bin:/usr/local/bin"',
        "",
    ]
    patterns = []
    for cmd in COMMANDS:
        if not cmd.sudo:
            continue  # read-only commands run without sudo — not in the policy
        for pat in command_patterns(cmd, cfg):
            patterns.append(" ".join(pat))

    # One NOPASSWD directive listing every enumerated command.
    lines.append(f"{cfg.operator} ALL=(root) NOPASSWD: \\")
    for i, pat in enumerate(patterns):
        sep = ", \\" if i < len(patterns) - 1 else ""
        lines.append(f"    {pat}{sep}")
    lines.append("")
    return "\n".join(lines)


# Slot kind -> the shell predicate used to validate that token in the shim.
_SLOT_PREDICATE = {
    "version": "is_version",
    "name": "is_name",
    "git_url": "is_giturl",
    "keep": "is_int",
    "tail": "is_int",
}


def _shim_token_specs(cmd: Command, cfg: DeployConfig) -> list:
    """Arity variants for a command; each variant is a list of token specs.

    A token spec is either a literal string or a ('slot', kind) tuple. The order
    mirrors remote.build_remote_tokens exactly so the shim matches the real argv.
    """
    base = []
    if cmd.sudo:
        base += ["sudo", "-n"]
    base += [_binary(cmd, cfg)] + list(cmd.subcommand)
    for flag in cmd.flags:
        if isinstance(flag, FlagValue):
            base.append(("slot", flag.slot.kind))
        else:
            base.append(flag)
    if cmd.install_path:
        base += ["--path", cfg.syrvis_home]

    if cmd.positional is None:
        return [base]
    with_pos = base + ["--", ("slot", cmd.positional.kind)]
    if cmd.positional.optional:
        return [with_pos, list(base)]
    return [with_pos]


def _match_clause(specs: list) -> str:
    """Build the `[ ... ]` test that matches an argv against one token-spec list.

    Requires exact argument count and validates each token: literals by string
    equality, slots by their kind predicate. Positional params use ${N} so N>9
    works in POSIX sh.
    """
    tests = [f"[ $# -eq {len(specs)} ]"]
    for i, spec in enumerate(specs, start=1):
        if isinstance(spec, tuple):  # ('slot', kind)
            tests.append(f'{_SLOT_PREDICATE[spec[1]]} "${{{i}}}"')
        else:
            tests.append(f'[ "${{{i}}}" = "{spec}" ]')
    return " && ".join(tests)


def render_shim(cfg: DeployConfig = DEFAULT) -> str:
    """A POSIX-sh forced command that re-validates $SSH_ORIGINAL_COMMAND.

    Defense in depth (G13): independent of the client, it (1) whitelists the
    allowed character set — rejecting every shell metacharacter, glob, quote and
    control char in one check; (2) refuses embedded newlines; (3) disables
    globbing (set -f) and word-splits into argv; then (4) requires the argv to
    match one enumerated command shape EXACTLY — exact argument count, literals
    by equality, and each slot value by a strict per-kind regex. Only then is the
    original argv exec'd.
    """
    header = [
        "#!/bin/sh",
        "# /usr/local/bin/syrvis-mcp-shim  (install 0755 root:root)",
        "# GENERATED from syrviscore_mcp/commands.py by deploy/gen.py — do not hand-edit.",
        "# Forced command for the MCP operator SSH key. Re-validates the requested",
        "# command independently of client-side checks (defense in depth, G13).",
        "",
        "is_version() { printf '%s' \"$1\" | LC_ALL=C grep -Eq '^v?[0-9]+\\.[0-9]+\\.[0-9]+$'; }",
        "is_name()    { printf '%s' \"$1\" | LC_ALL=C grep -Eq '^[a-z0-9][a-z0-9_-]{0,63}$'; }",
        "is_int()     { printf '%s' \"$1\" | LC_ALL=C grep -Eq '^[0-9]{1,7}$'; }",
        "is_giturl()  { printf '%s' \"$1\" | LC_ALL=C grep -Eq "
        "'^(https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._/-]+|git@[A-Za-z0-9.-]+:"
        "[A-Za-z0-9._/-]+|ssh://git@[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._/-]+)$'; }",
        "",
        'cmd="$SSH_ORIGINAL_COMMAND"',
        'if [ -z "$cmd" ]; then',
        '  echo "syrvis-mcp-shim: interactive login not permitted" >&2; exit 1',
        "fi",
        "",
        "# Character whitelist: only this safe alphabet may appear anywhere. This",
        "# rejects every shell metacharacter, glob (* ? [ ]), quote, $, backtick,",
        "# and control char in one shot — far stronger than a denylist.",
        "if printf '%s' \"$cmd\" | LC_ALL=C grep -q '[^A-Za-z0-9 ._@:/-]'; then",
        '  echo "syrvis-mcp-shim: forbidden character" >&2; exit 1',
        "fi",
        "# Reject embedded newlines (grep is line-oriented and would miss them).",
        'if [ "$(printf \'%s\' "$cmd" | wc -l | tr -d \' \')" != "0" ]; then',
        '  echo "syrvis-mcp-shim: forbidden newline" >&2; exit 1',
        "fi",
        "",
        "set -f          # no pathname expansion when we word-split",
        "set -- $cmd     # split into argv (safe: charset whitelisted above)",
        "",
        "# Match the argv against exactly one enumerated command shape.",
    ]

    body = []
    seen = set()
    for cmd in COMMANDS:
        for specs in _shim_token_specs(cmd, cfg):
            key = tuple(str(s) for s in specs)
            if key in seen:
                continue
            seen.add(key)
            body.append(f"if {_match_clause(specs)}; then")
            body.append('  set -f; exec "$@"')
            body.append("fi")

    footer = [
        'echo "syrvis-mcp-shim: command not on allowlist" >&2',
        "exit 1",
        "",
    ]
    return "\n".join(header + body + footer)


SUDOERS_INSTALL_PATH = "/etc/sudoers.d/syrviscore-mcp"


def render_provision(
    cfg: DeployConfig,
    pubkey: str,
    from_cidr: str = "192.168.0.0/16",
    fullname: str = "SyrvisCore MCP operator",
) -> str:
    """Render a self-contained POSIX-sh provisioning script to run on the NAS.

    Everything it needs is baked in (the exact sudoers policy, the exact shim,
    the operator's public key, and the real paths for this deployment), so the
    operator copies ONE file to the NAS and runs it with sudo. It is idempotent,
    validates the sudoers with visudo BEFORE installing it, and backs up every
    system file it touches to a timestamped directory first.
    """
    pubkey = pubkey.strip()
    # A real SSH public key is `type base64 [comment]` — reject anything with a
    # character that could break out of the single-quoted AUTHLINE (or inject
    # into the generated script): quotes, $, backtick, newline, etc.
    if not pubkey or not re.match(r"^[A-Za-z0-9 @._:/=+-]+$", pubkey):
        raise ValueError("pubkey is empty or contains unsafe characters for a public key line")
    if not re.match(r"^(ssh-|ecdsa-|sk-)", pubkey):
        raise ValueError("pubkey does not look like an SSH public key (expected ssh-ed25519 ...)")
    # from_cidr is operator-supplied too; keep it to an IP/CIDR shape.
    if not re.match(r"^[0-9A-Fa-f:.]+(/\d{1,3})?$", from_cidr):
        raise ValueError(f"invalid --from CIDR: {from_cidr!r}")
    authline = f'restrict,command="{cfg.shim_path}",from="{from_cidr}" {pubkey}'
    sudoers_body = render_sudoers(cfg).rstrip("\n")
    shim_body = render_shim(cfg).rstrip("\n")

    # Only this small block is an f-string (scalar substitutions, no shell '{}').
    # Everything else is a raw string so shell ${...}/$(...)/{ } stay literal;
    # the sudoers/shim bodies are inserted as raw heredoc content.
    var_block = f"""#!/bin/sh
# manual_mcp_account_provision.sh
# GENERATED by syrviscore_mcp/deploy/gen.py for this specific NAS deployment.
#
# Provisions the least-privilege MCP operator account on the Synology NAS:
#   1. create the '{cfg.operator}' user (if missing) and ensure docker access
#   2. install the enumerated NOPASSWD sudoers policy (visudo-validated)
#   3. install the forced-command shim
#   4. install the operator SSH key, locked to that shim
# It captures the TRUE pre-install state once and writes a rollback.sh that
# reverts exactly. Idempotent; safe to re-run.
#
# RUN ON THE NAS as root:
#     sudo sh manual_mcp_account_provision.sh            # apply
#     sudo sh manual_mcp_account_provision.sh --dry-run  # show what it would do
set -u

OPERATOR="{cfg.operator}"
FULLNAME="{fullname}"
SUDOERS_PATH="{SUDOERS_INSTALL_PATH}"
SHIM_PATH="{cfg.shim_path}"
SYRVIS_HOME="{cfg.syrvis_home}"
AUTHLINE='{authline}'
"""

    helpers = r"""
DRYRUN=0
[ "${1:-}" = "--dry-run" ] && DRYRUN=1

STAMP=$(date +%Y%m%d-%H%M%S)
STATE_DIR="/var/log/syrviscore-mcp-provision"
ORIG_DIR="$STATE_DIR/original"        # true pre-install state, captured ONCE
MANIFEST="$ORIG_DIR/manifest"
ROLLBACK="$STATE_DIR/rollback.sh"
RUN_DIR="$STATE_DIR/$STAMP"           # this run's scratch (temp files)

STEP="starting"
trap 'ec=$?; if [ "$ec" -ne 0 ]; then printf "\n== ABORTED during: %s (exit %s)\n== Nothing further was changed. If any step ran, revert with: sudo sh %s\n" "$STEP" "$ec" "$ROLLBACK" >&2; fi' EXIT

say()  { printf '%s\n' "== $*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# run(): fail-fast (no `set -e` footguns) — every mutating command goes through it.
run()  { if [ "$DRYRUN" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else eval "$*" || die "command failed: $*"; fi; }

# capture_original PATH — record the TRUE pre-install state of PATH exactly once,
# so a second run never overwrites the first run's captured original. Feeds the
# generated rollback.sh: restore the saved copy, or remove a file we created.
capture_original() {
    p="$1"
    marker="$ORIG_DIR/.captured$p"
    if [ -e "$marker" ]; then return 0; fi
    if [ "$DRYRUN" = 1 ]; then printf '   [dry-run] capture original of %s\n' "$p"; return 0; fi
    mkdir -p "$(dirname "$marker")" || die "mkdir failed: $(dirname "$marker")"
    : > "$marker"
    if [ -e "$p" ]; then
        dest="$ORIG_DIR/files$p"
        mkdir -p "$(dirname "$dest")" || die "mkdir failed"
        cp -a "$p" "$dest" || die "could not back up $p"
        say "backed up existing $p"
        printf 'restore %s %s\n' "$dest" "$p" >> "$MANIFEST"
    else
        printf 'remove %s\n' "$p" >> "$MANIFEST"
    fi
    write_rollback
}

# write_rollback — (re)generate rollback.sh from the manifest after each capture,
# so a mid-run abort still leaves a correct, runnable rollback.
write_rollback() {
    [ -f "$MANIFEST" ] || return 0
    tmp="$ROLLBACK.new"
    {
        printf '#!/bin/sh\n# Revert SyrvisCore MCP provisioning to the pre-install state.\nset -u\n'
        while read -r action a b; do
            case "$action" in
                restore) printf "cp -a '%s' '%s'\n" "$a" "$b" ;;
                remove)  printf "rm -f '%s'\n" "$a" ;;
            esac
        done < "$MANIFEST"
        printf 'echo "Reverted sudoers/shim/authorized_keys to their pre-install state."\n'
        printf 'echo "If this run created the %s account, remove it via DSM Control Panel."\n' "$OPERATOR"
    } > "$tmp"
    mv "$tmp" "$ROLLBACK"
    chmod 700 "$ROLLBACK"
}

# --- Preflight -------------------------------------------------------------
STEP="preflight"
[ "$(id -u)" = "0" ] || die "run this as root: sudo sh $0"
for t in visudo synouser synogroup getent install cp; do
    command -v "$t" >/dev/null 2>&1 || die "required tool not found: $t"
done
if [ ! -x "$SYRVIS_HOME/bin/syrvis" ]; then
    say "WARNING: $SYRVIS_HOME/bin/syrvis not found — is SYRVIS_HOME correct? Regenerate with --home if your install is on another volume."
fi
run "mkdir -p '$ORIG_DIR' '$RUN_DIR'"
run "chmod 700 '$STATE_DIR' '$RUN_DIR'"
say "State + backups under $STATE_DIR (rollback script: $ROLLBACK)"

# --- 1. Operator account ---------------------------------------------------
STEP="create operator account"
if id "$OPERATOR" >/dev/null 2>&1; then
    say "user '$OPERATOR' already exists — leaving it as-is"
else
    say "creating '$OPERATOR' (SSH-key-only; a strong random password is set but never used)"
    # DSM synouser --add: username password "full name" expired(0|1) mail AppPrivilege
    RANDPW=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    if [ "$DRYRUN" = 1 ]; then
        printf '   [dry-run] synouser --add %s <random-pw> "%s" 0 "" ""\n' "$OPERATOR" "$FULLNAME"
    elif ! synouser --add "$OPERATOR" "$RANDPW" "$FULLNAME" 0 "" "" >/dev/null 2>&1; then
        unset RANDPW
        die "synouser failed. Create the user in DSM Control Panel > User (do NOT add to the administrators group), then re-run."
    fi
    unset RANDPW
fi

# --- 2. docker access (read-only tools read the docker socket) -------------
STEP="docker group membership"
if ! getent group docker >/dev/null 2>&1; then
    say "docker group missing — creating it"
    run "synogroup --add docker"
fi
if id "$OPERATOR" 2>/dev/null | tr ',' '\n' | grep -q '(docker)'; then
    say "'$OPERATOR' is already in the docker group"
else
    say "adding '$OPERATOR' to docker (memberadd does NOT replace existing members)"
    if [ "$DRYRUN" = 1 ]; then
        printf '   [dry-run] synogroup --memberadd docker %s\n' "$OPERATOR"
    elif ! synogroup --memberadd docker "$OPERATOR" >/dev/null 2>&1; then
        say "WARNING: 'synogroup --memberadd' failed (not all DSM versions have it)."
        say "  Add '$OPERATOR' to the 'docker' group via DSM Control Panel > Group, then re-run."
    elif ! id "$OPERATOR" 2>/dev/null | tr ',' '\n' | grep -q '(docker)'; then
        say "WARNING: '$OPERATOR' is still not in the docker group after memberadd."
        say "  The read-only MCP tools (status/verify/service_list) need docker socket access;"
        say "  add '$OPERATOR' to 'docker' via DSM Control Panel > Group before using them."
    fi
fi

# --- 3. sudoers policy (validate BEFORE installing, re-validate after) ------
STEP="install sudoers policy"
say "installing sudoers policy -> $SUDOERS_PATH"
TMP_SUDOERS="$RUN_DIR/sudoers.new"
"""

    # sudoers heredoc — raw body, must not be expanded
    sudoers_block = (
        'if [ "$DRYRUN" = 1 ]; then\n'
        "  printf '   [dry-run] would write, visudo-validate, install %s\\n' \"$SUDOERS_PATH\"\n"
        "else\n"
        "cat > \"$TMP_SUDOERS\" <<'SYRVIS_SUDOERS_EOF'\n" + sudoers_body + "\nSYRVIS_SUDOERS_EOF\n"
        '  visudo -cf "$TMP_SUDOERS" || die "generated sudoers failed visudo validation (not installed)"\n'
        "fi\n"
    )

    sudoers_install = r"""capture_original "$SUDOERS_PATH"
run "install -m 0440 -o root -g root '$TMP_SUDOERS' '$SUDOERS_PATH'"
# Re-validate what actually landed, so a torn write can't silently break sudo.
if [ "$DRYRUN" != 1 ]; then
    visudo -cf "$SUDOERS_PATH" || { rm -f "$SUDOERS_PATH"; die "landed sudoers failed validation; removed to keep sudo working"; }
fi

# --- 4. forced-command shim ------------------------------------------------
STEP="install forced-command shim"
say "installing forced-command shim -> $SHIM_PATH"
TMP_SHIM="$RUN_DIR/shim.new"
"""

    shim_block = (
        'if [ "$DRYRUN" = 1 ]; then\n'
        "  printf '   [dry-run] would install %s\\n' \"$SHIM_PATH\"\n"
        "else\n"
        "cat > \"$TMP_SHIM\" <<'SYRVIS_SHIM_EOF'\n" + shim_body + "\nSYRVIS_SHIM_EOF\n"
        "fi\n"
    )

    key_and_finish = r"""capture_original "$SHIM_PATH"
run "install -m 0755 -o root -g root '$TMP_SHIM' '$SHIM_PATH'"

# --- 5. operator SSH key, locked to the shim (ADDITIVE) --------------------
STEP="install operator SSH key"
HOMEDIR=$(getent passwd "$OPERATOR" | cut -d: -f6)
[ -n "$HOMEDIR" ] || die "could not resolve home dir for '$OPERATOR' (enable the user-home service in DSM, then re-run). sudoers + shim are installed; run '$ROLLBACK' to revert if you want to start over."
SSH_DIR="$HOMEDIR/.ssh"
AUTH="$SSH_DIR/authorized_keys"
say "installing operator key -> $AUTH (forced command + source restriction, keeping any other keys)"
capture_original "$AUTH"
if [ "$DRYRUN" = 1 ]; then
    printf '   [dry-run] would add the forced-command key to %s (preserving other keys)\n' "$AUTH"
else
    run "mkdir -p '$SSH_DIR'"
    NEW="$SSH_DIR/.authorized_keys.syrvis.new"
    # Keep every OTHER key; drop any stale line for THIS shim; then add ours.
    if [ -f "$AUTH" ]; then
        grep -vF 'command="'"$SHIM_PATH"'"' "$AUTH" > "$NEW" 2>/dev/null || : > "$NEW"
    else
        : > "$NEW"
    fi
    printf '%s\n' "$AUTHLINE" >> "$NEW"
    run "mv '$NEW' '$AUTH'"
    run "chown -R '$OPERATOR' '$SSH_DIR'"
    run "chmod 700 '$SSH_DIR'"
    run "chmod 600 '$AUTH'"
fi

# --- Done ------------------------------------------------------------------
STEP="done"
say "provisioning complete."
cat <<VERIFY

Rollback script: $ROLLBACK   (reverts sudoers/shim/authorized_keys exactly)
Run-scratch:     $RUN_DIR

Verify the boundary (run 'ssh' from your Mac; all but 'sudo -l' should be DENIED):
  ssh syrvis-nas 'id'                     # rejected by the forced-command shim
  ssh syrvis-nas 'sudo -n /bin/sh'        # denied by the sudoers policy
  ssh syrvis-nas 'sudo -l'                # should list ONLY the enumerated commands

To undo everything this installed:
  sudo sh $ROLLBACK
  # then remove the '$OPERATOR' account via DSM Control Panel if this script created it
VERIFY
"""
    return var_block + helpers + sudoers_block + sudoers_install + shim_block + key_and_finish


def _cfg_from_args(args) -> DeployConfig:
    kwargs = {}
    if args.home:
        kwargs["syrvis_home"] = args.home.rstrip("/")
    if args.operator:
        kwargs["operator"] = args.operator
    if args.syrvisctl:
        kwargs["syrvisctl_path"] = args.syrvisctl
    if args.shim_path:
        kwargs["shim_path"] = args.shim_path
    return DeployConfig(**kwargs)


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="syrviscore_mcp.deploy.gen", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("sudoers", "shim"):
        p = sub.add_parser(name, help=f"render the {name} to stdout")
        _add_path_opts(p)

    p_prov = sub.add_parser("provision", help="render the NAS provisioning script to stdout")
    _add_path_opts(p_prov)
    p_prov.add_argument(
        "--pubkey", required=True, help="path to the operator SSH public key (.pub)"
    )
    p_prov.add_argument(
        "--from",
        dest="from_cidr",
        default="192.168.0.0/16",
        help="source CIDR allowed to use the key",
    )

    p_check = sub.add_parser("check", help="verify committed artifacts match the defaults")
    p_check.add_argument("dir", help="the deploy/ directory to check")

    args = parser.parse_args(argv[1:])

    if args.cmd == "check":
        base = Path(args.dir)
        sud = (base / "sudoers.d" / "syrviscore-mcp").read_text()
        shim = (base / "ssh" / "syrvis-mcp-shim").read_text()
        ok = sud == render_sudoers() and shim == render_shim()
        print("OK" if ok else "DRIFT")
        return 0 if ok else 1

    cfg = _cfg_from_args(args)
    if args.cmd == "sudoers":
        sys.stdout.write(render_sudoers(cfg))
    elif args.cmd == "shim":
        sys.stdout.write(render_shim(cfg))
    elif args.cmd == "provision":
        pubkey = Path(args.pubkey).read_text()
        sys.stdout.write(render_provision(cfg, pubkey, from_cidr=args.from_cidr))
    return 0


def _add_path_opts(p) -> None:
    p.add_argument("--home", help="SYRVIS_HOME on the NAS (default /volume1/syrviscore)")
    p.add_argument("--operator", help="operator username (default syrvis-operator)")
    p.add_argument("--syrvisctl", help="absolute path to syrvisctl on the NAS")
    p.add_argument("--shim-path", dest="shim_path", help="where the shim is installed on the NAS")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
