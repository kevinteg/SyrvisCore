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

import sys
from pathlib import Path

# Allow running both as a module and as a script.
try:
    from syrviscore_mcp.commands import COMMANDS, Command, FlagValue
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from syrviscore_mcp.commands import COMMANDS, Command, FlagValue

# Canonical production deployment paths. Regenerate with different paths if the
# NAS uses a volume other than /volume1 (see the MCP README / NAS-verify steps).
OPERATOR = "syrvis-operator"
SYRVIS_WRAPPER = "/volume1/syrviscore/bin/syrvis"
SYRVISCTL_PATH = "/var/packages/syrviscore/target/venv/bin/syrvisctl"
SYRVIS_HOME = "/volume1/syrviscore"
SHIM_PATH = "/usr/local/bin/syrvis-mcp-shim"


def _binary(cmd: Command) -> str:
    return SYRVIS_WRAPPER if cmd.cli == "syrvis" else SYRVISCTL_PATH


def _fixed_tokens(cmd: Command) -> list:
    """The command tokens with slot values replaced by '*' (no sudo prefix)."""
    tokens = [_binary(cmd)] + list(cmd.subcommand)
    for flag in cmd.flags:
        tokens.append("*" if isinstance(flag, FlagValue) else flag)
    if cmd.install_path:
        tokens += ["--path", SYRVIS_HOME]
    return tokens


def command_patterns(cmd: Command) -> list:
    """All accepted argv shapes for a command (list of token-lists).

    Optional positionals expand to two forms (present with '-- *', and absent).
    """
    fixed = _fixed_tokens(cmd)
    if cmd.positional is None:
        return [fixed]
    with_pos = fixed + ["--", "*"]
    if cmd.positional.optional:
        return [with_pos, list(fixed)]
    return [with_pos]


def render_sudoers() -> str:
    lines = [
        "# /etc/sudoers.d/syrviscore-mcp  (install 0440 root:root; validate: visudo -cf)",
        "# GENERATED from syrviscore_mcp/commands.py by deploy/gen.py — do not hand-edit.",
        "# Enumerated NOPASSWD boundary for the MCP operator. Only the privileged",
        "# SyrvisCore subcommands below are permitted; everything else needs a human.",
        "",
        f'Defaults:{OPERATOR}  !requiretty, env_reset, secure_path="/usr/bin:/bin:/usr/local/bin"',
        "",
    ]
    patterns = []
    for cmd in COMMANDS:
        if not cmd.sudo:
            continue  # read-only commands run without sudo — not in the policy
        for pat in command_patterns(cmd):
            patterns.append(" ".join(pat))

    # One NOPASSWD directive listing every enumerated command.
    lines.append(f"{OPERATOR} ALL=(root) NOPASSWD: \\")
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


def _shim_token_specs(cmd: Command) -> list:
    """Arity variants for a command; each variant is a list of token specs.

    A token spec is either a literal string or a ('slot', kind) tuple. The order
    mirrors remote.build_remote_tokens exactly so the shim matches the real argv.
    """
    base = []
    if cmd.sudo:
        base += ["sudo", "-n"]
    base += [_binary(cmd)] + list(cmd.subcommand)
    for flag in cmd.flags:
        if isinstance(flag, FlagValue):
            base.append(("slot", flag.slot.kind))
        else:
            base.append(flag)
    if cmd.install_path:
        base += ["--path", SYRVIS_HOME]

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


def render_shim() -> str:
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
        for specs in _shim_token_specs(cmd):
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


def main(argv):
    if len(argv) >= 2 and argv[1] == "sudoers":
        sys.stdout.write(render_sudoers())
        return 0
    if len(argv) >= 2 and argv[1] == "shim":
        sys.stdout.write(render_shim())
        return 0
    if len(argv) >= 3 and argv[1] == "check":
        base = Path(argv[2])
        sud = (base / "sudoers.d" / "syrviscore-mcp").read_text()
        shim = (base / "ssh" / "syrvis-mcp-shim").read_text()
        ok = sud == render_sudoers() and shim == render_shim()
        print("OK" if ok else "DRIFT")
        return 0 if ok else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
