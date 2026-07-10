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
    """Run the shim with exec neutered to 'echo ALLOW'; return (rc, output)."""
    src = SHIM.read_text().replace("exec $cmd;;", "echo ALLOW;;")
    r = subprocess.run(
        ["sh", "-c", src],
        env={"SSH_ORIGINAL_COMMAND": original_command, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


DENY = [
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
]

ALLOW = [
    "/volume1/syrviscore/bin/syrvis status --json",
    "/volume1/syrviscore/bin/syrvis verify --smoke --json",
    "/var/packages/syrviscore/target/venv/bin/syrvisctl list --json",
    "sudo -n /volume1/syrviscore/bin/syrvis service stop -- gollum",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl activate -- 0.2.0",
    "sudo -n /volume1/syrviscore/bin/syrvis service add -- https://github.com/u/r.git",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl cleanup --keep 2 -y",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl install -y --path /volume1/syrviscore",
    "sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl install -y --path /volume1/syrviscore -- 0.2.0",
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
