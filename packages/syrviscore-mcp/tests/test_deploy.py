"""
Deploy-artifact tests: sudoers validity (visudo) and forced-command shim
allow/deny behavior run under a real /bin/sh.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SUDOERS = DEPLOY / "sudoers.d" / "syrviscore-mcp"
SHIM = DEPLOY / "ssh" / "syrvis-mcp-shim"


def _visudo():
    for cand in ("/usr/sbin/visudo", "/sbin/visudo", shutil.which("visudo")):
        if cand and Path(cand).exists():
            return cand
    return None


@pytest.mark.skipif(_visudo() is None, reason="visudo not available")
def test_sudoers_valid():
    r = subprocess.run([_visudo(), "-cf", str(SUDOERS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_sudoers_has_no_dangerous_entries():
    text = SUDOERS.read_text()
    # Command-specific tokens (avoid false positives like 'env_reset').
    for forbidden in (
        "--purge",
        "syrvis reset",
        "syrvis clean",
        "syrvisctl restore",
        "syrvis restore",
        "--wheel",
        "--no-verify",
        "--force",
        "--clean",
        "/bin/sh",
        " docker ",
        " git ",
    ):
        assert forbidden not in text, f"sudoers must not permit {forbidden!r}"
    # env_reset present, no SYRVIS_HOME= carried through
    assert "env_reset" in text
    assert "SYRVIS_HOME=" not in text


def _run_shim(original_command: str):
    """Run the shim with exec neutered to 'echo ALLOW; exit 0'; return (rc, output)."""
    src = SHIM.read_text().replace('exec "$@"', "echo ALLOW; exit 0")
    r = subprocess.run(
        ["sh", "-c", src],
        env={"SSH_ORIGINAL_COMMAND": original_command, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


DENY = [
    # secret set red-team vectors
    "sudo -n /volume1/syrviscore/bin/syrvis secret set -- Immich_DB",  # uppercase -> is_name fails
    "sudo -n /volume1/syrviscore/bin/syrvis secret set -- immich-db extra",  # extra token -> wrong arity
    "sudo -n /volume1/syrviscore/bin/syrvis secret set immich-db",  # missing '--' separator
    "sudo -n /volume1/syrviscore/bin/syrvis secret set",  # missing positional entirely
    "/volume1/syrviscore/bin/syrvis secret set -- immich-db",  # missing sudo -> wrong shape
    "id",
    "sudo -n /bin/sh",
    "docker ps",
    "sudo -n /volume1/syrviscore/bin/syrvis service stop -- foo; reboot",
    "sudo -n /volume1/syrviscore/bin/syrvis service add -- --upload-pack=/bin/sh",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl uninstall -y -- $(id)",
    "sudo -n /volume1/syrviscore/bin/syrvis service stop -- foo bar",
    "/volume1/syrviscore/bin/syrvis status",  # missing --json -> off allowlist
    "sudo -n /volume1/syrviscore/bin/syrvis restore -- x",  # restore not exposed
    "sudo -n /volume1/syrviscore/bin/syrvis service remove -- foo --purge -y",  # purge
    # red-team vectors (F3/F4/F5/F6): glob, double '--', extra flag/token
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl cleanup --keep 1 * -y",
    "sudo -n /volume1/syrviscore/bin/syrvis service add -- --evil -- https://github.com/u/r.git",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl cleanup --keep 1 --extra-flag -y",
    "/volume1/syrviscore/bin/syrvis logs -n 5 -f -- gollum",  # extra token
    # service_run red-team: bad exposure, missing flag group, invalid subdomain
    "sudo -n /volume1/syrviscore/bin/syrvis service run --image ghcr.io/a/b:1.0 "
    "--subdomain cq --exposure public --port 80 -- cq",  # exposure not internal|tunnel
    "sudo -n /volume1/syrviscore/bin/syrvis service run --image ghcr.io/a/b:1.0 "
    "--subdomain cq --exposure tunnel -- cq",  # missing --port group -> wrong arity
    "sudo -n /volume1/syrviscore/bin/syrvis service run --image ghcr.io/a/b:1.0 "
    "--subdomain Bad_Sub --exposure tunnel --port 80 -- cq",  # subdomain not a DNS label
    # services.d red-team: bad prune policy, prune without -y, no-sudo variant,
    # non-lowercase booleans, adopt without --json (off allowlist)
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --json -y --prune everything",
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --json --prune purge",
    "/volume1/syrviscore/bin/syrvis reconcile --json -y",  # missing sudo -> wrong shape
    "sudo -n /volume1/syrviscore/bin/syrvis service declare --image ghcr.io/a/b:1.0 "
    "--subdomain cq --exposure tunnel --port 80 --enabled TRUE --critical false --json -- cq",
    "sudo -n /volume1/syrviscore/bin/syrvis service declare --image ghcr.io/a/b:1.0 "
    "--subdomain cq --exposure tunnel --port 80 --enabled true --critical 1 --json -- cq",
    "sudo -n /volume1/syrviscore/bin/syrvis service adopt -- gollum",
]

ALLOW = [
    # secret set allow: valid service name, correct shape
    "sudo -n /volume1/syrviscore/bin/syrvis secret set -- immich-db",
    "sudo -n /volume1/syrviscore/bin/syrvis secret set -- immich-server",
    "/volume1/syrviscore/bin/syrvis status --json",
    "/volume1/syrviscore/bin/syrvis verify --smoke --json",
    "/volume1/syrviscore/bin/syrvis stack hostnames --json",
    "/var/packages/syrviscore/target/venv/bin/syrvisctl list --json",
    "sudo -n /volume1/syrviscore/bin/syrvis service stop -- gollum",
    "sudo -n /volume1/syrviscore/bin/syrvis stack apply",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl activate -- 0.2.0",
    "sudo -n /volume1/syrviscore/bin/syrvis service add -- https://github.com/u/r.git",
    "sudo -n /volume1/syrviscore/bin/syrvis service run "
    "--image ghcr.io/acme/cyberquill:1.4.0 --subdomain cyberquill "
    "--exposure tunnel --port 8080 -- cyberquill",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl cleanup --keep 2 -y",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl install -y --path /volume1/syrviscore",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl install -y --path /volume1/syrviscore -- 0.2.0",
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --dry-run --json",
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --json -y",
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --json -y --prune stop",
    "sudo -n /volume1/syrviscore/bin/syrvis reconcile --json -y --prune purge",
    "sudo -n /volume1/syrviscore/bin/syrvis service declare "
    "--image ghcr.io/acme/cyberquill:1.4.0 --subdomain cyberquill "
    "--exposure tunnel --port 8080 --enabled true --critical false --json -- cyberquill",
    "sudo -n /volume1/syrviscore/bin/syrvis service adopt --json -- gollum",
]


@pytest.mark.parametrize("cmd", DENY)
def test_shim_denies(cmd):
    rc, out = _run_shim(cmd)
    assert rc != 0, f"expected denial for: {cmd} (got: {out})"
    assert "ALLOW" not in out


@pytest.mark.parametrize("cmd", ALLOW)
def test_shim_allows(cmd):
    rc, out = _run_shim(cmd)
    assert rc == 0 and out == "ALLOW", f"expected allow for: {cmd} (got rc={rc}: {out})"


def test_shim_rejects_empty_command():
    rc, out = _run_shim("")
    assert rc != 0


def test_shim_disables_globbing_and_has_no_bare_exec_cmd():
    text = SHIM.read_text()
    assert "set -f" in text  # F3: no pathname expansion
    assert "exec $cmd" not in text  # never the unquoted form
    assert 'exec "$@"' in text  # exec the validated argv


@pytest.mark.parametrize(
    "cmd",
    [
        # glob chars, quotes, $ etc. are rejected by the charset whitelist
        "/volume1/syrviscore/bin/syrvis status --json ; echo x",
        "/volume1/syrviscore/bin/syrvis status --json*",
        "/volume1/syrviscore/bin/syrvis status --json?",
    ],
)
def test_shim_charset_whitelist(cmd):
    rc, out = _run_shim(cmd)
    assert rc != 0
