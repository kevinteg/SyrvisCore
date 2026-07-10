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


def _case_pattern(shape: str) -> str:
    """Turn a command shape into a valid `case` pattern.

    Case patterns cannot contain unquoted spaces, and quoting a `*` would
    disable the glob. So we single-quote each literal segment and leave the
    `*` slot markers as bare globs: "literal "*" literal".
    """
    segments = shape.split("*")
    out = []
    for i, seg in enumerate(segments):
        if seg:
            # our shapes never contain a single quote; safe to wrap directly
            out.append("'" + seg + "'")
        if i < len(segments) - 1:
            out.append("*")
    return "".join(out) if out else "''"


def render_shim() -> str:
    """A POSIX-sh forced command that re-validates $SSH_ORIGINAL_COMMAND.

    Read-only commands arrive as '<binary> ...'; privileged as 'sudo -n <binary> ...'.
    We hard-reject shell metacharacters (the injection boundary), then allowlist
    the exact command shapes (glob '*' for slot values). sudo itself does the
    strict arg-by-arg match for the privileged ones.
    """
    header = [
        "#!/bin/sh",
        "# /usr/local/bin/syrvis-mcp-shim  (install 0755 root:root)",
        "# GENERATED from syrviscore_mcp/commands.py by deploy/gen.py — do not hand-edit.",
        "# Forced command for the MCP operator SSH key. Re-validates the requested",
        "# command independently of client-side checks (defense in depth, G13).",
        "",
        'cmd="$SSH_ORIGINAL_COMMAND"',
        'if [ -z "$cmd" ]; then',
        '  echo "syrvis-mcp-shim: interactive login not permitted" >&2; exit 1',
        "fi",
        "",
        "# Injection boundary: reject any shell metacharacter (grep bracket class",
        "# treats these as literals) or control character, including newlines.",
        "if printf '%s' \"$cmd\" | LC_ALL=C grep -q '[;&|`$()<>\\!]'; then",
        '  echo "syrvis-mcp-shim: forbidden character" >&2; exit 1',
        "fi",
        "if printf '%s' \"$cmd\" | LC_ALL=C grep -q '[[:cntrl:]]'; then",
        '  echo "syrvis-mcp-shim: control character" >&2; exit 1',
        "fi",
        "",
        "# If the command carries a '--' positional, its value must be exactly one",
        "# safe token: alnum-leading, no leading '-' (blocks e.g. git --upload-pack),",
        "# no spaces (blocks extra args the sudo '*' would otherwise permit).",
        'case "$cmd" in',
        "  *' -- '*)",
        "    val=${cmd##* -- }",
        '    case "$val" in',
        '      *" "*) echo "syrvis-mcp-shim: extra args after --" >&2; exit 1;;',
        "    esac",
        "    if ! printf '%s' \"$val\" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._@:/-]*$'; then",
        '      echo "syrvis-mcp-shim: invalid positional value" >&2; exit 1',
        "    fi;;",
        "esac",
        "",
        "# Allowlist of accepted command shapes (glob * = one slot value).",
        'case "$cmd" in',
    ]

    body = []
    seen = set()
    for cmd in COMMANDS:
        prefix = "sudo -n " if cmd.sudo else ""
        for pat in command_patterns(cmd):
            shape = prefix + " ".join(pat)
            if shape in seen:
                continue
            seen.add(shape)
            body.append(f"  {_case_pattern(shape)})")
            body.append("    exec $cmd;;")

    footer = [
        "  *)",
        '    echo "syrvis-mcp-shim: command not on allowlist" >&2; exit 1;;',
        "esac",
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
